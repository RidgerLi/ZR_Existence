"""
本地遥测与审计仓（Telemetry / audit store）
Author: ZerolanLiveRobot

一个进程内共享的 SQLite 库，集中存放"可以拿来做回顾、打分、二次开发"的数据：

    llm_calls      每一次大模型调用的 token 用量（真实或估算）
    turns          每一轮对话（用户说了什么 / 她回了什么 / 耗时 / 是否被打断 / token）
    reviews        你对某轮回复的打分（1~5 + 标签 + 备注），每轮最多一条
    proposals      根据打分生成的"修改意见"，等待你通过 / 忽略
    capability_runs 本地能力的调用记录（成功/失败/耗时/结果摘要）
    journals       每日日报的元信息（正文落在 resources/memory/journal/*.md）

为什么用 SQLite：
    - 单文件、无额外进程，和 Chroma 一样是本地嵌入式；
    - 之后要做微调数据集，直接 SQL 导出 JSONL 即可；
    - 比 JSONL 更适合"随机抽未打分的句子""按天统计"这类查询。

线程模型：
    一个连接 + 一把递归锁。所有公开函数都可从任意线程调用（Flask 请求线程、大脑线程、
    LLM 线程都会进来）。WAL 模式，读写不互相阻塞太久。
"""

import json
import os
import random
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from loguru import logger

_lock = threading.RLock()
_conn: Optional[sqlite3.Connection] = None
_db_path: Optional[str] = None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    model TEXT,
    source TEXT,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    estimated INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    user_text TEXT NOT NULL,
    reply TEXT NOT NULL,
    latency_ms INTEGER DEFAULT 0,
    interrupted INTEGER DEFAULT 0,
    tokens_prompt INTEGER DEFAULT 0,
    tokens_completion INTEGER DEFAULT 0,
    meta TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reviews (
    turn_id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    score INTEGER NOT NULL,
    tags TEXT DEFAULT '',
    comment TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    decided_ts TEXT
);
CREATE TABLE IF NOT EXISTS capability_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    name TEXT NOT NULL,
    ok INTEGER NOT NULL,
    ms INTEGER DEFAULT 0,
    detail TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS journals (
    day TEXT PRIMARY KEY,
    ts TEXT NOT NULL,
    path TEXT NOT NULL,
    summary TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);
CREATE INDEX IF NOT EXISTS idx_calls_ts ON llm_calls(ts);
CREATE INDEX IF NOT EXISTS idx_runs_name ON capability_runs(name);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def estimate_tokens(text: str) -> int:
    """字符数估算 token（中文约 1 字 ≈ 0.6~0.8 token，英文约 4 字符 1 token）。

    只在服务商没有返回 usage 时兜底，精度大约 ±25%，库里会标 estimated=1 以便区分。
    """
    return max(1, int(len(text or "") / 1.6)) if text else 0


def init(db_path: str) -> None:
    """打开（或创建）数据库。重复调用只生效一次。"""
    global _conn, _db_path
    with _lock:
        if _conn is not None:
            return
        _db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        _conn = sqlite3.connect(db_path, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.executescript(_SCHEMA)
        _conn.commit()
        logger.info(f"Telemetry store ready at {db_path}")


def _db() -> sqlite3.Connection:
    if _conn is None:
        init(os.path.join("resources", "data", "robot.db"))
    return _conn  # type: ignore[return-value]


def _exec(sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _db().execute(sql, params)
        _db().commit()
        return cur


def _query(sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    with _lock:
        return [dict(r) for r in _db().execute(sql, params).fetchall()]


# ---------------------------------------------------------------- LLM tokens
def record_llm_call(model: str, source: str,
                    prompt_tokens: int, completion_tokens: int,
                    estimated: bool = False) -> int:
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    cur = _exec(
        "INSERT INTO llm_calls(ts, model, source, prompt_tokens, completion_tokens, total_tokens, estimated) "
        "VALUES (?,?,?,?,?,?,?)",
        (_now(), model or "", source or "chat", prompt_tokens, completion_tokens,
         prompt_tokens + completion_tokens, 1 if estimated else 0),
    )
    return int(cur.lastrowid)


def last_llm_id() -> int:
    rows = _query("SELECT COALESCE(MAX(id), 0) AS m FROM llm_calls")
    return int(rows[0]["m"]) if rows else 0


def sum_tokens_since(mark_id: int, source: Optional[str] = None) -> Tuple[int, int]:
    """统计 mark_id 之后（不含）的调用 token 之和，可按来源过滤。"""
    sql = ("SELECT COALESCE(SUM(prompt_tokens),0) AS p, COALESCE(SUM(completion_tokens),0) AS c "
           "FROM llm_calls WHERE id > ?")
    params: List[Any] = [int(mark_id)]
    if source:
        sql += " AND source = ?"
        params.append(source)
    rows = _query(sql, params)
    return (int(rows[0]["p"]), int(rows[0]["c"])) if rows else (0, 0)


def token_totals(day: Optional[str] = None, source: Optional[str] = None) -> Dict[str, Any]:
    sql = "SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c, " \
          "COALESCE(SUM(estimated),0) est, COUNT(*) n FROM llm_calls WHERE 1=1"
    params: List[Any] = []
    if day:
        sql += " AND ts LIKE ?"
        params.append(f"{day}%")
    if source:
        sql += " AND source = ?"
        params.append(source)
    rows = _query(sql, params)
    r = rows[0] if rows else {"p": 0, "c": 0, "est": 0, "n": 0}
    return {"prompt_tokens": int(r["p"]), "completion_tokens": int(r["c"]),
            "total_tokens": int(r["p"]) + int(r["c"]), "calls": int(r["n"]),
            "estimated_calls": int(r["est"])}


# ---------------------------------------------------------------- turns
def record_turn(user_text: str, reply: str, latency_ms: int = 0,
                interrupted: bool = False, tokens_prompt: int = 0,
                tokens_completion: int = 0, meta: Optional[Dict[str, Any]] = None,
                ts: Optional[str] = None) -> int:
    cur = _exec(
        "INSERT INTO turns(ts, user_text, reply, latency_ms, interrupted, tokens_prompt, tokens_completion, meta) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (ts or _now(), user_text or "", reply or "", int(latency_ms or 0),
         1 if interrupted else 0, int(tokens_prompt or 0), int(tokens_completion or 0),
         json.dumps(meta or {}, ensure_ascii=False)),
    )
    return int(cur.lastrowid)


def count_turns() -> int:
    rows = _query("SELECT COUNT(*) AS n FROM turns")
    return int(rows[0]["n"]) if rows else 0


def backfill_turns(pairs: Sequence[Tuple[str, str, Optional[str]]]) -> int:
    """把已有历史（user, assistant, ts）补录进库，用于第一次上线时立即有可打分的样本。"""
    added = 0
    with _lock:
        existing = {r["user_text"] + "\x00" + r["reply"] for r in _query("SELECT user_text, reply FROM turns")}
    for user_text, reply, ts in pairs:
        if not user_text or not reply:
            continue
        key = user_text + "\x00" + reply
        if key in existing:
            continue
        record_turn(user_text, reply, ts=ts or _now())
        existing.add(key)
        added += 1
    if added:
        logger.info(f"Telemetry backfilled {added} turn(s) from existing history.")
    return added


def sample_unreviewed_turns(limit: int = 5, days: int = 14) -> List[Dict[str, Any]]:
    since = (datetime.now() - timedelta(days=max(1, days))).strftime("%Y-%m-%d")
    rows = _query(
        "SELECT t.*, r.score FROM turns t LEFT JOIN reviews r ON r.turn_id = t.id "
        "WHERE r.turn_id IS NULL AND t.ts >= ? ORDER BY RANDOM() LIMIT ?",
        (since, max(1, int(limit))),
    )
    return rows


def reviewed_turn_ids() -> List[int]:
    return [int(r["turn_id"]) for r in _query("SELECT turn_id FROM reviews")]


# ---------------------------------------------------------------- reviews
def save_review(turn_id: int, score: int, tags: str = "", comment: str = "") -> None:
    score = max(1, min(5, int(score)))
    _exec(
        "INSERT INTO reviews(turn_id, ts, score, tags, comment) VALUES (?,?,?,?,?) "
        "ON CONFLICT(turn_id) DO UPDATE SET ts=excluded.ts, score=excluded.score, "
        "tags=excluded.tags, comment=excluded.comment",
        (int(turn_id), _now(), score, tags or "", comment or ""),
    )


def list_reviews(limit: int = 50) -> List[Dict[str, Any]]:
    return _query(
        "SELECT r.*, t.user_text, t.reply FROM reviews r LEFT JOIN turns t ON t.id = r.turn_id "
        "ORDER BY r.ts DESC LIMIT ?", (max(1, int(limit)),))


def review_stats(day: Optional[str] = None) -> Dict[str, Any]:
    sql = "SELECT COUNT(*) n, COALESCE(AVG(score),0) avg FROM reviews"
    params: List[Any] = []
    if day:
        sql += " WHERE ts LIKE ?"
        params.append(f"{day}%")
    rows = _query(sql, params)
    return {"count": int(rows[0]["n"]), "avg": round(float(rows[0]["avg"]), 2)} if rows else {"count": 0, "avg": 0.0}


# ---------------------------------------------------------------- proposals
def add_proposal(title: str, body: str) -> int:
    cur = _exec("INSERT INTO proposals(ts, title, body, status) VALUES (?,?,?,'pending')",
                (_now(), title or "建议", body or ""))
    return int(cur.lastrowid)


def list_proposals(status: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM proposals"
    params: List[Any] = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return _query(sql, params)


def decide_proposal(proposal_id: int, approved: bool) -> Optional[Dict[str, Any]]:
    status = "accepted" if approved else "rejected"
    _exec("UPDATE proposals SET status=?, decided_ts=? WHERE id=?",
          (status, _now(), int(proposal_id)))
    rows = _query("SELECT * FROM proposals WHERE id=?", (int(proposal_id),))
    return rows[0] if rows else None


# ---------------------------------------------------------------- capabilities
def record_capability_run(name: str, ok: bool, ms: int, detail: str = "") -> None:
    _exec("INSERT INTO capability_runs(ts, name, ok, ms, detail) VALUES (?,?,?,?,?)",
          (_now(), name, 1 if ok else 0, int(ms or 0), (detail or "")[:2000]))


def capability_stats(name: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = ("SELECT name, COUNT(*) runs, SUM(ok) ok_runs, MAX(ts) last_ts, "
           "COALESCE(AVG(ms),0) avg_ms FROM capability_runs ")
    params: List[Any] = []
    if name:
        sql += "WHERE name = ? "
        params.append(name)
    sql += "GROUP BY name ORDER BY runs DESC"
    return _query(sql, params)


def recent_capability_runs(limit: int = 20, name: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM capability_runs"
    params: List[Any] = []
    if name:
        sql += " WHERE name = ?"
        params.append(name)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, int(limit)))
    return _query(sql, params)


# ---------------------------------------------------------------- journals
def save_journal(day: str, path: str, summary: str = "") -> None:
    _exec("INSERT INTO journals(day, ts, path, summary) VALUES (?,?,?,?) "
          "ON CONFLICT(day) DO UPDATE SET ts=excluded.ts, path=excluded.path, summary=excluded.summary",
          (day, _now(), path, summary or ""))


def get_journal(day: str) -> Optional[Dict[str, Any]]:
    rows = _query("SELECT * FROM journals WHERE day=?", (day,))
    return rows[0] if rows else None


def list_journals(limit: int = 14) -> List[Dict[str, Any]]:
    return _query("SELECT * FROM journals ORDER BY day DESC LIMIT ?", (max(1, int(limit)),))


# ---------------------------------------------------------------- stats / export
def day_stats(day: str) -> Dict[str, Any]:
    turns = _query("SELECT COUNT(*) n FROM turns WHERE ts LIKE ?", (f"{day}%",))
    reviews = review_stats(day)
    tokens = token_totals(day)
    runs = _query("SELECT COUNT(*) n FROM capability_runs WHERE ts LIKE ?", (f"{day}%",))
    return {
        "day": day,
        "turns": int(turns[0]["n"]),
        "reviews": reviews["count"],
        "avg_score": reviews["avg"],
        "tokens": tokens,
        "capability_runs": int(runs[0]["n"]),
    }


def export_reviews_jsonl(path: str) -> int:
    """导出"已打分"的数据，供之后做微调数据集。返回条数。"""
    rows = _query(
        "SELECT r.turn_id, r.ts, r.score, r.tags, r.comment, t.user_text, t.reply "
        "FROM reviews r JOIN turns t ON t.id = r.turn_id ORDER BY r.ts")
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)
