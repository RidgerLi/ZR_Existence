"""控制面注册表的单元测试。"""

import pytest

from framework import control


@pytest.fixture(autouse=True)
def _clean_registry():
    saved = list(control.available())
    yield
    for name in control.available():
        if name not in saved:
            control.unregister(name)


def test_register_and_call():
    control.register("test.echo", lambda p: {"text": p.get("text")})
    r = control.call("test.echo", {"text": "hi"})
    assert r == {"ok": True, "result": {"text": "hi"}}
    assert control.has("test.echo")
    assert "test.echo" in control.available()


def test_unknown_action():
    r = control.call("test.does_not_exist")
    assert r["ok"] is False
    assert "unknown action" in r["error"]


def test_exception_becomes_error_dict():
    def boom(_p):
        raise ValueError("bad input")

    control.register("test.boom", boom)
    r = control.call("test.boom")
    assert r["ok"] is False
    assert "bad input" in r["error"]


def test_unregister():
    control.register("test.temp", lambda p: 1)
    control.unregister("test.temp")
    assert not control.has("test.temp")
