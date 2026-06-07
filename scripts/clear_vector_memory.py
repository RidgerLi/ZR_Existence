"""清空长期记忆向量库（L2b）。

背景：早期归档把"全量对话原文/台词"塞进了向量库，成了脏数据——检索时会把过期情节
当成"关于对方你记得的事"回拼进 prompt，造成时间线矛盾。换用 `extract_durable_facts`
只抽耐久事实后，需要把旧的脏数据清掉，让向量库从干净状态重新积累。

用法（在项目根目录执行）：
    python scripts/clear_vector_memory.py            # 先显示条数并询问确认
    python scripts/clear_vector_memory.py --yes      # 跳过确认，直接清空
    python scripts/clear_vector_memory.py --dry-run  # 只看条数，不删

会根据 config.yaml 自动定位后端（chroma / milvus）、持久化目录与 collection 名，
所以清的就是 bot 实际使用的那个库。
"""

from __future__ import annotations

import argparse
import os
import sys

# 允许从项目根目录或 scripts/ 目录直接运行。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from loguru import logger

from manager.config_manager import get_config


def _resolve_target() -> tuple[str, str, str]:
    """从 config 解析（后端, 定位信息, collection 名）。"""
    config = get_config()
    vec_cfg = config.pipeline.vec_db
    backend = getattr(vec_cfg, "backend", "chroma")
    collection = getattr(config.system.memory, "collection_name", "history_collection")
    if backend == "milvus":
        locator = getattr(vec_cfg.milvus, "host", "<milvus>")
    else:
        locator = getattr(vec_cfg, "chroma_persist_dir", "resources/memory/chroma")
    return backend, locator, collection


def _clear_chroma(persist_dir: str, collection: str, dry_run: bool, assume_yes: bool) -> int:
    import chromadb

    if not os.path.isdir(persist_dir):
        logger.warning(f"Chroma 持久化目录不存在：{persist_dir}（无需清理）。")
        return 0

    client = chromadb.PersistentClient(path=persist_dir)
    try:
        coll = client.get_collection(name=collection)
    except Exception:
        logger.info(f"collection '{collection}' 不存在（可能从未写入过），无需清理。")
        return 0

    count = coll.count()
    logger.info(f"后端=chroma  目录={persist_dir}  collection='{collection}'  当前记录数={count}")
    if count == 0:
        logger.info("已经是空的，无需清理。")
        return 0
    if dry_run:
        logger.info("[dry-run] 不做任何删除。")
        return 0

    if not assume_yes:
        ans = input(f"确认删除 collection '{collection}' 的全部 {count} 条记录？(yes/N) ").strip().lower()
        if ans not in ("y", "yes"):
            logger.info("已取消。")
            return 0

    # 直接删除整个 collection 最干净；下次 bot 调用 get_or_create_collection 会自动重建空表。
    client.delete_collection(name=collection)
    logger.info(f"已删除 collection '{collection}'（清空 {count} 条记录）。")
    return count


def _clear_milvus(collection: str, dry_run: bool, assume_yes: bool) -> int:
    logger.warning(
        "当前后端为 milvus（远程 ZerolanCore 服务）。本脚本不直接连远程 Milvus 做 drop，"
        f"请在 ZerolanCore 侧删除 collection '{collection}'，或把 backend 切回 chroma 后再运行本脚本。"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="清空长期记忆向量库（L2b）。")
    parser.add_argument("--yes", action="store_true", help="跳过确认，直接清空。")
    parser.add_argument("--dry-run", action="store_true", help="只显示条数，不删除。")
    args = parser.parse_args()

    backend, locator, collection = _resolve_target()
    logger.info(f"目标向量库：backend={backend}  locator={locator}  collection={collection}")

    if backend == "milvus":
        _clear_milvus(collection, args.dry_run, args.yes)
    else:
        _clear_chroma(locator, collection, args.dry_run, args.yes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
