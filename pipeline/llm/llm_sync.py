from typing import Generator

from openai import OpenAI
from requests import Response
from typeguard import typechecked
from zerolan.data.pipeline.llm import LLMQuery, LLMPrediction, RoleEnum, Conversation

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


class LLMSyncPipeline(CommonModelPipeline):

    def __init__(self, config: LLMPipelineConfig):
        super().__init__(config)
        # When `openai_format` is True, we delegate all chat requests to the OpenAI SDK
        # and forward `config.model_id` verbatim as the `model` parameter. This works
        # for any OpenAI-compatible service (DeepSeek / Kimi / Doubao / OpenAI / ...),
        # so adding a new provider or a new model version requires NO code change here.
        self._is_openai_format = config.openai_format
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

    @typechecked
    def predict(self, query: LLMQuery) -> LLMPrediction | None:
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
            query.history.append(Conversation(role=RoleEnum.user, content=query.text))
            query.history.append(Conversation(role=RoleEnum.assistant, content=resp))
            return LLMPrediction(response=resp, history=query.history)
        else:
            return super().predict(query)

    @typechecked
    def stream_predict(self, query: LLMQuery, chunk_size: int | None = None) -> Generator[str, None, None]:
        """
        Stream the LLM response. Yields incremental text deltas (str) as they arrive.

        Important: this generator does NOT update `query.history`. Callers that need
        history persistence should accumulate the deltas themselves and update the
        chat history once the generator is exhausted.
        """
        assert isinstance(query, LLMQuery)
        if self._is_openai_format:
            messages = _to_openai_format(query)
            stream = self._remote_model.chat.completions.create(
                model=self.model_id,
                messages=messages,
                stream=True,
                **self._extra_kwargs,
            )
            for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                content = getattr(delta, "content", None)
                if content:
                    yield content
        else:
            yield from super().stream_predict(query, chunk_size=chunk_size)

    def parse_prediction(self, response: Response) -> LLMPrediction:
        json_val = response.content
        return LLMPrediction.model_validate_json(json_val)
