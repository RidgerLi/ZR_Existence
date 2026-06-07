from pydantic import BaseModel, Field

from pipeline.db.milvus.milvus_sync import MilvusDatabaseConfig


#########
# VecDB #
#########

class VectorDBConfig(BaseModel):
    enable: bool = Field(default=True, description="Whether the Vector Database is enabled.")
    backend: str = Field(default="chroma",
                         description="Vector store backend: 'chroma' (local embedded, Windows-friendly, no torch) "
                                     "or 'milvus' (HTTP to a ZerolanCore Milvus service).")
    chroma_persist_dir: str = Field(default="resources/memory/chroma",
                                    description="Persistence directory for the local ChromaDB backend.")
    milvus: MilvusDatabaseConfig = Field(default=MilvusDatabaseConfig(),
                                         description="Configuration for the Milvus Database (used when backend='milvus').")
