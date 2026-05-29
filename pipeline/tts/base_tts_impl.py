from abc import ABC, abstractmethod
from zerolan.data.pipeline.tts import TTSQuery, TTSPrediction
from pydantic import BaseModel

class BaseTTSImpl(ABC):
    @abstractmethod
    def predict(self, query: TTSQuery) -> TTSPrediction | None: ...

    @abstractmethod
    def stream_predict(self, query: TTSQuery, chunk_size: int | None = None):...

    def parse_query(self, query: any) -> any:
        if isinstance(query, BaseModel):
            query_dict = query.model_dump()
            return query_dict
        else:
            raise NotImplementedError("Unsupported `Query` object parsing method: not a subclass of BaseModel.")

