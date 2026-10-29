import textwrap

import pytest

from framework.capabilities import CapabilityManager

HELLO = textwrap.dedent('''
NAME = "hello"
DESCRIPTION = "test capability"

def run(args):
    return "ok:" + str(args.get("x", 0))
''')

BYE = textwrap.dedent('''
NAME = "bye"
DESCRIPTION = "another one"

def run(args):
    raise RuntimeError("nope")
''')


@pytest.fixture()
def manager(tmp_path):
    caps = tmp_path / "caps"
    caps.mkdir()
    (caps / "hello.py").write_text(HELLO, encoding="utf-8")
    return CapabilityManager(str(caps), str(tmp_path / "state.json")), caps


def test_load_and_run(manager):
    mgr, _ = manager
    assert mgr.names() == ["hello"]
    info = mgr.list_info()
    assert info[0]["enabled"] is True and info[0]["runs"] == 0
    assert mgr.run("hello", {"x": 7})["result"] == "ok:7"


def test_toggle_persisted(manager, tmp_path):
    mgr, _ = manager
    mgr.set_enabled("hello", False)
    assert mgr.run and mgr.is_enabled("hello") is False
    with pytest.raises(PermissionError):
        mgr.run("hello", {})
    # 重新构造：状态应保持
    mgr2 = CapabilityManager(str(tmp_path / "caps"), str(tmp_path / "state.json"))
    assert mgr2.is_enabled("hello") is False


def test_failure_recorded(manager):
    mgr, caps = manager
    (caps / "bye.py").write_text(BYE, encoding="utf-8")
    mgr.maybe_reload()
    assert "bye" in mgr.names()
    with pytest.raises(RuntimeError):
        mgr.run("bye", {})
    runs = mgr.recent_runs(10, name="bye")
    assert runs and runs[0]["ok"] == 0


def test_new_file_auto_discovered(manager):
    mgr, caps = manager
    assert mgr.names() == ["hello"]
    (caps / "late.py").write_text(
        'NAME = "late"\nDESCRIPTION = "added later"\n\ndef run(args):\n    return 1\n',
        encoding="utf-8")
    # 目录 mtime 可能同秒：显式 reload 一次，等价于"面板刷新"
    mgr.reload()
    assert "late" in mgr.names()


def test_unknown_capability(manager):
    mgr, _ = manager
    with pytest.raises(KeyError):
        mgr.run("missing", {})
