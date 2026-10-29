import json

from framework import telemetry


def test_token_recording_and_sum():
    mark = telemetry.last_llm_id()
    telemetry.record_llm_call("m", "chat", 100, 50, estimated=False)
    telemetry.record_llm_call("m", "journal", 20, 10, estimated=True)
    p, c = telemetry.sum_tokens_since(mark, source="chat")
    assert (p, c) == (100, 50)
    totals = telemetry.token_totals()
    assert totals["total_tokens"] >= 180


def test_estimation_helper():
    assert telemetry.estimate_tokens("") == 0
    # 中文约 0.7 token/字，8 个字 ≈ 5~6 token（估算器只保证同数量级）
    assert 3 <= telemetry.estimate_tokens("这是一段中文文本") <= 12


def test_turn_review_roundtrip():
    turn_id = telemetry.record_turn("你好", "在呢", latency_ms=123, tokens_prompt=10, tokens_completion=5,
                                    meta={"route": "test"})
    assert turn_id > 0
    assert telemetry.count_turns() >= 1

    review = telemetry.save_review(turn_id, 5, "很好", "自然")
    rows = [r for r in telemetry.list_reviews(50) if r["turn_id"] == turn_id]
    assert rows and rows[0]["score"] == 5 and rows[0]["comment"] == "自然"

    stats = telemetry.review_stats()
    assert stats["count"] >= 1


def test_sampling_excludes_reviewed():
    tid = telemetry.record_turn("抽我", "抽我回复")
    telemetry.save_review(tid, 3, "", "")
    sampled = telemetry.sample_unreviewed_turns(50, days=3650)
    assert all(row["id"] != tid for row in sampled)


def test_proposals_lifecycle():
    pid = telemetry.add_proposal("更短", "回复尽量两句话内。")
    pending = telemetry.list_proposals("pending")
    assert any(p["id"] == pid for p in pending)
    decided = telemetry.decide_proposal(pid, True)
    assert decided["status"] == "accepted"
    assert all(p["id"] != pid for p in telemetry.list_proposals("pending"))


def test_capability_runs_and_stats():
    telemetry.record_capability_run("unit_test_cap", True, 12, "ok")
    telemetry.record_capability_run("unit_test_cap", False, 30, "boom")
    stats = [s for s in telemetry.capability_stats("unit_test_cap")]
    assert stats and stats[0]["runs"] == 2 and int(stats[0]["ok_runs"]) == 1


def test_journal_upsert(tmp_path):
    day = "2099-01-01"
    path = tmp_path / "2099-01-01.md"
    path.write_text("# hi", encoding="utf-8")
    telemetry.save_journal(day, str(path), "turns=3")
    assert telemetry.get_journal(day)["summary"] == "turns=3"
    telemetry.save_journal(day, str(path), "turns=4")
    assert telemetry.get_journal(day)["summary"] == "turns=4"
    assert any(j["day"] == day for j in telemetry.list_journals(5))


def test_export_reviews(tmp_path):
    tid = telemetry.record_turn("导出我", "导出回复")
    telemetry.save_review(tid, 4, "tag", "note")
    out = tmp_path / "reviews.jsonl"
    n = telemetry.export_reviews_jsonl(str(out))
    assert n >= 1
    lines = [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines()]
    assert lines and "score" in lines[0] and "user_text" in lines[0]


def test_day_stats_shape():
    stats = telemetry.day_stats("2099-01-02")
    assert {"turns", "reviews", "avg_score", "tokens", "capability_runs"} <= set(stats)
    assert "total_tokens" in stats["tokens"]
