"""示例能力：返回当前本地时间。"""

from datetime import datetime

NAME = "time_now"
DESCRIPTION = "返回当前本地日期、星期与时间（不需要大模型）。"


def run(args: dict) -> str:
    now = datetime.now()
    weekday = "周" + "一二三四五六日"[now.weekday()]
    return now.strftime(f"%Y-%m-%d {weekday} %H:%M:%S")
