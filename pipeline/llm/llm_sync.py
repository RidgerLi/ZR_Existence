from typing import Generator

from loguru import logger
from openai import OpenAI
from requests import Response
from typeguard import typechecked
from zerolan.data.pipeline.llm import LLMQuery, LLMPrediction, RoleEnum, Conversation

from framework import telemetry
from pipeline.base.base_sync import CommonModelPipeline
from pipeline.llm.config import LLMPipelineConfig


def _to_openai_format(query: LLMQuery):
    messages = []
    for chat in query.history:
        messages.append({
            "role": chat.role,
            "content": chat.content
        })
    messages.append({
        "role": "user",
        "content": query.text
    })
    return messages


def _messages_chars(messages) -> int:
    return sum(len(str(m.get("content", ""))) for m in messages)


class LLMSyncPipeline(CommonModelPipeline):

    def __init__(self, config: LLMPipelineConfig):
        super().__init__(config)
        # When `openai_format` is True, we delegate all chat requests to the OpenAI SDK
        # and forward `config.model_id` verbatim as the `model` parameter. This works
        # for any OpenAI-compatible service (DeepSeek / Kimi / Doubao / OpenAI / ...),
        # so adding a new provider or a new model version requires NO code change here.
        self._is_openai_format = config.openai_format
        # 流式调用是否支持 `stream_options.include_usage`：None=还没试过，True=支持，False=不支持。
        # 不支持时自动退回"按字符估算"，不影响对话。
        self._stream_usage_supported = None
        if self._is_openai_format:
            assert config.predict_url and config.stream_predict_url, "Please provide `predict_url` or `stream_predict_url`"
            base_url = config.predict_url if config.predict_url else config.stream_predict_url
            self._remote_model = OpenAI(api_key=config.api_key, base_url=base_url)
            # Provider-specific options forwarded verbatim to every `chat.completions.create`
            # call (both blocking and streaming). Only non-None values are collected so we
            # never override the SDK's own defaults with an explicit None.
            self._extra_kwargs = {}
            if config.reasoning_effort is not None:
                self._extra_kwargs["reasoning_effort"] = config.reasoning_effort
            if config.extra_body is not None:
                self._extra_kwargs["extra_body"] = config.extra_body

    def _record_usage(self, source: str, usage, prompt_chars: int, completion_chars: int) -> None:
        """把一次调用的 token 用量记进本地遥测库；服务商没给 usage 时按字符估算。"""
        try:
            if usage is not None:
                p = int(getattr(usage, "prompt_tokens", 0) or 0)
                c = int(getattr(usage, "completion_tokens", 0) or 0)
                telemetry.record_llm_call(self.model_id, source, p, c, estimated=False)
            else:
                telemetry.record_llm_call(
                    self.model_id, source,
                    telemetry.estimate_tokens("x" * prompt_chars) if prompt_chars else 0,
                    telemetry.estimate_tokens("x" * completion_chars) if completion_chars else 0,
                    estimated=True,
                )
        except Exception as e:
            logger.debug(f"Record LLM usage skipped: {e}")

    @typechecked
    def predict(self, query: LLMQuery, source: str = "chat") -> LLMPrediction | None:
        assert isinstance(query, LLMQuery)
        if self._is_openai_format:
            messages = _to_openai_format(query)
            completion = self._remote_model.chat.completions.create(
                model=self.model_id,
                messages=messages,
                stream=False,
                **self._extra_kwargs,
            )
            resp = completion.choices[0].message.content
            self._record_usage(source, getattr(completion, "usage", None),
                               _messages_chars(messages), len(resp or ""))
            query.history.append(Conversation(role=RoleEnum.user, content=query.text))
            query.history.append(Conversation(role=RoleEnum.assistant, content=resp))
            return LLMPrediction(response=resp, history=query.history)
        else:
            prediction = super().predict(query)
            try:
                self._record_usage(source, None,
                                   len(query.text or ""),
                                   len(getattr(prediction, "response", "") or ""))
            except Exception:
                pass
            return prediction

    @typechecked
    def stream_predict(self, query: LLMQuery, chunk_size: int | None = None,
                       source: str = "chat") -> Generator[str, None, None]:
        """
        Stream the LLM response. Yields incremental text deltas (str) as they arrive.

        Important: this generator does NOT update `query.history`. Callers that need
        history persistence should accumulate the deltas themselves and update the
        chat history once the generator is exhausted.

        顺便统计 token：优先用服务商在最后一个 chunk 里返回的 usage；如果服务商不支持
        `stream_options.include_usage`，自动退回按字符估算（estimated=1）。
        """
        assert isinstance(query, LLMQuery)
        if self._is_openai_format:
            messages = _to_openai_format(query)
            kwargs = dict(model=self.model_id, messages=messages, stream=True, **self._extra_kwargs)
            stream = None
            if self._stream_usage_supported is not False:
                try:
                    stream = self._remote_model.chat.completions.create(
                        stream_options={"include_usage": True}, **kwargs)
                    self._stream_usage_supported = True
                except Exception as e:
                    if self._stream_usage_supported is None:
                        logger.warning(f"Stream usage not supported by provider, estimating instead: {e}")
                        self._stream_usage_supported = False
                    else:
                        raise
            if stream is None:
                stream = self._remote_model.chat.completions.create(**kwargs)

            usage = None
            out_chars = 0
            try:
                for chunk in stream:
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        usage = chunk_usage
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None)
                    if content:
                        out_chars += len(content)
                        yield content
            finally:
                # 即使调用方中途放弃（插话打断/异常），也把已产生的用量记下来。
                self._record_usage(source, usage, _messages_chars(messages), out_chars)
        else:
            out_chars = 0
            for delta in super().stream_predict(query, chunk_size=chunk_size):
                out_chars += len(delta or "")
                yield delta
            self._record_usage(source, None, len(query.text or "") * 2, out_chars)

    def parse_prediction(self, response: Response) -> LLMPrediction:
        json_val = response.content
        return LLMPrediction.model_validate_json(json_val)
