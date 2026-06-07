"""
本地嵌入式向量库（ChromaDB）—— Windows 友好的 Milvus 替代
Author: ZerolanLiveRobot

Milvus Lite 不支持 Windows，所以在本机用 ChromaDB 作为长期记忆（L2b）的向量后端：
进程内嵌、持久化到本地目录，embedding 用 Chroma 默认的 ONNX MiniLM(all-MiniLM-L6-v2)，
不依赖 PyTorch。

与 `MilvusSyncPipeline` 保持**同样的接口与返回类型**（insert/search 收发 zerolan.data 的
MilvusInsert / MilvusInsertResult / MilvusQuery / MilvusQueryResult），因此 bot 侧关于长期
记忆的检索/入库代码无需任何改动即可切换后端。
"""

import os
import threading

from loguru import logger
from zerolan.data.pipeline.milvus import (MilvusInsert, MilvusInsertResult,
                                          MilvusQuery, MilvusQueryResult, QueryRow)

from pipeline.base.base_sync import AbstractPipeline


class ChromaLocalPipeline(AbstractPipeline):
    """本地 ChromaDB 向量库。config 需含 `enable` 与 `chroma_persist_dir`。"""

    def __init__(self, config):
        super().__init__(config)
        import chromadb

        self._persist_dir = getattr(config, "chroma_persist_dir", "resources/memory/chroma")
        os.makedirs(self._persist_dir, exist_ok=True)
        self._client = chromadb.PersistentClient(path=self._persist_dir)
        self._lock = threading.Lock()
        logger.info(f"Local ChromaDB vector store ready at '{self._persist_dir}'.")

    def _collection(self, name: str):
        # 默认 embedding function 即 ONNX MiniLM，无需 torch。
        return self._client.get_or_create_collection(name=name)

    def count(self, collection_name: str) -> int:
        """返回某 collection 当前的记录条数（供监控用）。失败返回 -1。"""
        try:
            with self._lock:
                return self._collection(collection_name).count()
        except Exception as e:
            logger.debug(f"Chroma count failed: {e}")
            return -1

    def insert(self, insert: MilvusInsert) -> MilvusInsertResult:
        assert isinstance(insert, MilvusInsert)
        docs = [r.text for r in insert.texts]
        if not docs:
            return MilvusInsertResult(insert_count=0, ids=[])
        ids = [str(r.id) for r in insert.texts]
        metadatas = [{"subject": r.subject} for r in insert.texts]
        with self._lock:
            coll = self._collection(insert.collection_name)
            # upsert：同 id 覆盖，避免重复入库报错。
            coll.upsert(documents=docs, ids=ids, metadatas=metadatas)
        return MilvusInsertResult(insert_count=len(docs), ids=[r.id for r in insert.texts])

    def search(self, query: MilvusQuery) -> MilvusQueryResult:
        assert isinstance(query, MilvusQuery)
        with self._lock:
            coll = self._collection(query.collection_name)
            count = coll.count()
            if count == 0:
                return MilvusQueryResult(result=[[]])
            res = coll.query(query_texts=[query.query], n_results=min(query.limit, count))

        docs = (res.get("documents") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        ids = (res.get("ids") or [[]])[0]
        rows = []
        for i, doc in enumerate(docs):
            try:
                rid = int(ids[i])
            except (ValueError, TypeError, IndexError):
                rid = i
            dist = float(dists[i]) if i < len(dists) else 0.0
            rows.append(QueryRow(id=rid, entity={"text": doc}, distance=dist))
        return MilvusQueryResult(result=[rows])
