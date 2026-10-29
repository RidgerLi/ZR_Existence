"""
每日日报（Journal）
Author: ZerolanLiveRobot

只读的"当天回顾"：把遥测数据（对话轮数、token、评分、能力调用）整理成一份 Markdown，
落在 `resources/memory/journal/YYYY-MM-DD.md`。

设计原则：**只记录，不修改任何配置/人设/记忆**。日报里可以有建议，但生效必须经过
"生成修改意见 → 你通过"这条链路（见 proposals）。

没有大模型可用时也能生成（只是少一段"观察与建议"），保证这个功能不会因为 API 挂了而失效。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional


def build_reflection_prompt(day: str, stats: Dict[str, Any], reviews: List[Dict[str, Any]]) -> str:
    lines = [
        "你在帮一个 AI 主播做每日复盘。下面是今天的数据，请用中文写 3~5 条简短观察。",
        "只描述你在数据里真的看到的东西，不要编造；不要输出 JSON；每条一行，以「- 」开头。",
        f"日期：{day}",
        f"对话轮数：{stats.get('turns')}；平均分：{stats.get('avg_score')}（{stats.get('reviews')} 条评分）；"
        f"token：prompt {stats.get('tokens', {}).get('prompt_tokens')} / completion {stats.get('tokens', {}).get('completion_tokens')}；"
        f"能力调用：{stats.get('capability_runs')} 次",
    ]
    if reviews:
        lines.append("最近的评分记录（分数越低说明越不满意）：")
        for r in reviews[:10]:
            lines.append(
                f"- {r.get('score')} 分｜标签：{r.get('tags') or '无'}｜备注：{r.get('comment') or '无'}｜"
                f"用户：{str(r.get('user_text') or '')[:60]}｜她：{str(r.get('reply') or '')[:60]}")
    else:
        lines.append("今天还没有评分记录。")
    return "\n".join(lines)


def render_markdown(day: str, stats: Dict[str, Any], reviews: List[Dict[str, Any]],
                    reflection: Optional[str] = None) -> str:
    tokens = stats.get("tokens") or {}
    out: List[str] = [
        f"# {day} 日报",
        "",
        f"> 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M')}　·　只读回顾，不修改任何设置",
        "",
        "## 概况",
        "",
        "| 指标 | 数值 |",
        "|---|---|",
        f"| 对话轮数 | {stats.get('turns', 0)} |",
        f"| 评分条数 / 平均分 | {stats.get('reviews', 0)} / {stats.get('avg_score', 0)} |",
        f"| Token（prompt / completion） | {tokens.get('prompt_tokens', 0)} / {tokens.get('completion_tokens', 0)} |",
        f"| 估算调用数（服务商未返回 usage） | {tokens.get('estimated_calls', 0)} |",
        f"| 本地能力调用 | {stats.get('capability_runs', 0)} |",
        "",
    ]

    out += ["## 今天的评分", ""]
    if reviews:
        for r in reviews[:20]:
            stars = "★" * int(r.get("score") or 0) + "☆" * (5 - int(r.get("score") or 0))
            tags = f"　`{r.get('tags')}`" if r.get("tags") else ""
            comment = f"　—　{r.get('comment')}" if r.get("comment") else ""
            out.append(f"### {stars}{tags}{comment}")
            out.append("")
            out.append(f"- 你：{str(r.get('user_text') or '').strip()[:300]}")
            out.append(f"- 她：{str(r.get('reply') or '').strip()[:300]}")
            out.append("")
    else:
        out += ["（今天还没有打分。面板里可以随机抽几句来打。）", ""]

    out += ["## 观察与建议", ""]
    if reflection and reflection.strip():
        out.append(reflection.strip())
    else:
        out.append("（今天没有生成观察：可能是没开大模型，或调用失败。数据部分仍然完整。）")
    out.append("")
    out.append("---")
    out.append("")
    out.append("> 想要把这里的建议变成实际调整：在面板的 Review 卡片里点「生成修改意见」，"
               "逐条通过 / 忽略。")
    return "\n".join(out)
