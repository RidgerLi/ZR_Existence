from loguru import logger

from framework import log_monitor


def test_category_mapping():
    assert log_monitor._category("devices.microphone") == "VAD"
    assert log_monitor._category("devices.keyboard") == "INPUT"
    assert log_monitor._category("framework.capabilities") == "CAP"
    assert log_monitor._category("framework.control") == "CONTROL"
    assert log_monitor._category("pipeline.llm.llm_sync") == "LLM"
    assert log_monitor._category("bot") == "BOT"
    assert log_monitor._category("something.else") == "OTHER"


def test_get_logs_filters():
    log_monitor.install_sink("DEBUG")
    token = "ZRL_LOG_TEST_TOKEN_9f3a"
    logger.info(f"{token} hello")
    logger.warning(f"{token} warn")
    rows = log_monitor.get_logs(q=token)
    assert len(rows) >= 2
    assert all(token in r["message"] for r in rows)
    # 分类过滤：测试模块不属于任何已知分类
    assert log_monitor.get_logs(category="LLM", q=token) == []
    # 级别过滤：WARNING 及以上只剩 warn
    warns = log_monitor.get_logs(level="WARNING", q=token)
    assert len(warns) == 1 and warns[0]["level"] == "WARNING"
    # limit 只取最后 N 条
    assert len(log_monitor.get_logs(q=token, limit=1)) == 1
