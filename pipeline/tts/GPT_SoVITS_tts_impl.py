import os.path
from http import HTTPStatus

import requests
from loguru import logger
from zerolan.data.pipeline.tts import TTSQuery, TTSPrediction

from pipeline.tts.base_tts_impl import BaseTTSImpl

class GPTSoVITSTTSImpl(BaseTTSImpl):
    # base_url默认值是从GPTSoVITSTTS的默认端口来的
    def __init__(self, base_url: str = "http://127.0.0.1:9880", default_language: str = "zh"):
        self._base_url = base_url.rstrip("/")
        self._default_language = default_language

    @staticmethod
    def _normalize_language(query_language: str | None, fallback: str):
        if not query_language or query_language.lower() == "auto":
            return fallback
        return query_language.lower()

    # 把之前设计的paylodd转换成GPTSoVITSTTS api吃的接口 payload
    def _build_payload(self, query: TTSQuery, streaming_mode: bool = False):
        # query之前的language是auto，而gpt那边吃zh，所以要做改变
        prompt_language = self._normalize_language(query.prompt_language, self._default_language)
        text_language = self._normalize_language(query.text_language, prompt_language)

        return {
            "text": query.text,
            "text_lang" : text_language,
            "ref_audio_path": query.refer_wav_path,
            "prompt_text": query.prompt_text or "",
            "prompt_lang": prompt_language,
            "media_type": query.audio_type or "wav",
            "streaming_mode": streaming_mode,
        }

    def predict(self, query: TTSQuery) -> TTSPrediction | None:
        assert isinstance(query, TTSQuery)
        if os.path.exists(query.refer_wav_path):
            query.refer_wav_path = os.path.abspath(query.refer_wav_path).replace("\\", "/")
        url = f"{self._base_url}"
        payload = self._build_payload(query)
        response = requests.post(url=url, json=payload)
        if response.status_code == HTTPStatus.OK:
            prediction = TTSPrediction(wave_data=response.content, audio_type=query.audio_type or "wav")
            return prediction
        else:
            logger.error(response.content)
            response.raise_for_status()

    def stream_predict(self, query: TTSQuery, chunk_size: int | None = None):
        # TTS-level streaming is intentionally NOT implemented. Low latency in the voice
        # pipeline is already achieved upstream by splitting the LLM output into clauses
        # and synthesizing each clause via `predict` (see `_emit_llm_prediction_streaming`
        # in bot.py). A separate chunk-level TTS stream would be redundant here.
        raise NotImplementedError(
            "GPTSoVITSTTSImpl does not support stream_predict; use `predict` instead. "
            "Streaming is handled upstream by clause-level splitting."
        )

