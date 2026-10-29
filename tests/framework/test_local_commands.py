"""本地命令解析的单元测试（纯函数，不需要麦克风/配置）。"""

import pytest

from framework.local_commands import parse_local_command


@pytest.mark.parametrize("text,action,arg", [
    # 模式
    ("安静", "mode.set", "focus"),
    ("安静一点。", "mode.set", "focus"),
    ("专注模式", "mode.set", "focus"),
    ("普通模式", "mode.set", "normal"),
    ("陪我聊天", "mode.set", "companion"),
    ("自动模式", "mode.set", "auto"),
    ("恢复", "mode.resume", ""),
    ("可以说话了", "mode.resume", ""),
    # 待办
    ("添加待办：买牛奶", "todo.add", "买牛奶"),
    ("加个待办 买牛奶", "todo.add", "买牛奶"),
    ("待办加买牛奶", "todo.add", "买牛奶"),
    ("把写周报加入待办", "todo.add", "写周报"),
    ("把 写周报 加入待办", "todo.add", "写周报"),
    ("完成待办 买牛奶", "todo.done", "买牛奶"),
    ("待办买牛奶完成了", "todo.done", "买牛奶"),
    ("删除待办 买牛奶", "todo.remove", "买牛奶"),
    ("把买牛奶从待办里删掉", "todo.remove", "买牛奶"),
    # 目标
    ("添加目标 每天跑步", "goal.add", "每天跑步"),
    ("删除目标 每天跑步", "goal.remove", "每天跑步"),
])
def test_parse_local_command(text, action, arg):
    assert parse_local_command(text) == (action, arg)


@pytest.mark.parametrize("text", [
    "",
    "   ",
    "今天天气不错",
    "帮我添加一个闹钟",          # 没有"待办"，不该误判
    "我在想怎么把这件事加入待办",  # 不是句式开头，交给 LLM
    "嗯嗯",
    "你还记得我说过什么吗",
])
def test_non_commands_return_none(text):
    assert parse_local_command(text) is None


def test_remove_has_priority_over_add():
    """'删除待办X' 里也含'待办'，不能被 add 规则先命中。"""
    assert parse_local_command("删除待办 买菜")[0] == "todo.remove"
