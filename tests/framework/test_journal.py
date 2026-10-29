from framework import journal


STATS = {"day": "2026-10-29", "turns": 12, "reviews": 2, "avg_score": 3.5,
         "tokens": {"prompt_tokens": 1000, "completion_tokens": 500,
                    "total_tokens": 1500, "estimated_calls": 0},
         "capability_runs": 3}

REVIEWS = [
    {"score": 2, "tags": "太长", "comment": "像小作文", "user_text": "今天好累", "reply": "（很长的一段）"},
    {"score": 5, "tags": "很好", "comment": "", "user_text": "晚安", "reply": "快去睡。"},
]


def test_render_contains_sections():
    md = journal.render_markdown("2026-10-29", STATS, REVIEWS, "- 回复偏长\n- 晚上更活跃")
    assert "# 2026-10-29 日报" in md
    assert "| 对话轮数 | 12 |" in md
    assert "Token（prompt / completion） | 1000 / 500" in md
    assert "★★☆☆☆" in md
    assert "像小作文" in md
    assert "回复偏长" in md


def test_render_without_reflection_still_works():
    md = journal.render_markdown("2026-10-30", {"turns": 0, "tokens": {}}, [], None)
    assert "今天还没有打分" in md
    assert "没有生成观察" in md


def test_prompt_includes_scores():
    prompt = journal.build_reflection_prompt("2026-10-29", STATS, REVIEWS)
    assert "只描述你在数据里真的看到的东西" in prompt
    assert "2 分" in prompt and "太长" in prompt
