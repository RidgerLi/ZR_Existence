"""垫话 JSON 脚本的单元测试：解析、兜底、按场景选句、避免连续重复。"""

import json

import pytest

from framework import backchannel as bc


def test_parse_supports_strings_and_objects():
    script = bc.parse_script({
        "phrases": [
            "嗯？",
            {"text": "怎么啦", "weight": 2.5, "tags": ["question"]},
        ]
    })
    assert [p.text for p in script.phrases] == ["嗯？", "怎么啦"]
    assert script.phrases[0].weight == 1.0
    assert script.phrases[0].tags == (bc.TAG_ANY,)
    assert script.phrases[1].weight == 2.5
    assert script.phrases[1].matches(bc.TAG_QUESTION)
    assert not script.phrases[1].matches(bc.TAG_STATEMENT)


def test_invalid_tag_falls_back_to_any():
    script = bc.parse_script({"phrases": [{"text": "在呢", "tags": ["nonsense"]}]})
    assert script.phrases[0].tags == (bc.TAG_ANY,)


def test_duplicate_and_blank_phrases_dropped():
    script = bc.parse_script({"phrases": ["嗯", "嗯", "  ", {"text": ""}]})
    assert [p.text for p in script.phrases] == ["嗯"]


def test_empty_or_invalid_script_returns_default():
    assert len(bc.parse_script({"phrases": []}).phrases) > 0
    assert len(bc.parse_script(None).phrases) > 0


def test_default_script_has_question_and_statement():
    script = bc.default_script()
    tags = {t for p in script.phrases for t in p.tags}
    assert bc.TAG_QUESTION in tags and bc.TAG_STATEMENT in tags


def test_question_heuristic():
    assert bc.looks_like_question("你在干嘛？")
    assert bc.looks_like_question("这个怎么用")
    assert bc.looks_like_question("是不是该睡了")
    assert not bc.looks_like_question("我今天写完论文了")


def test_choose_respects_tag():
    script = bc.parse_script({"phrases": [
        {"text": "嗯？", "tags": ["question"]},
        {"text": "嗯嗯", "tags": ["statement"]},
    ]})
    assert bc.choose_phrase(script, bc.TAG_QUESTION).text == "嗯？"
    assert bc.choose_phrase(script, bc.TAG_STATEMENT).text == "嗯嗯"


def test_choose_avoids_recent():
    script = bc.parse_script({"phrases": [
        {"text": "嗯？", "tags": ["question"]},
        {"text": "怎么啦", "tags": ["question"]},
    ]})
    for _ in range(20):
        assert bc.choose_phrase(script, bc.TAG_QUESTION, recent=["嗯？"]).text == "怎么啦"


def test_choose_returns_none_when_empty():
    assert bc.choose_phrase(bc.Script(phrases=[]), bc.TAG_ANY) is None


def test_ensure_file_creates_editable_json(tmp_path):
    path = tmp_path / "sub" / "backchannel.json"
    assert bc.ensure_file(path) is True
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["phrases"] and data["phrases"][0]["text"]
    # 再次调用不应覆盖用户的修改
    path.write_text('{"phrases": ["我在"]}', encoding="utf-8")
    assert bc.ensure_file(path) is False
    assert bc.load_script(path).phrases[0].text == "我在"


def test_load_missing_and_broken_file_falls_back(tmp_path):
    assert len(bc.load_script(tmp_path / "nope.json").phrases) > 0
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert len(bc.load_script(broken).phrases) > 0


def test_roundtrip_json():
    script = bc.parse_script({"phrases": [{"text": "在呢", "weight": 3, "tags": ["any"]}]})
    dumped = json.loads(bc.script_to_json(script))
    assert dumped["phrases"][0] == {"text": "在呢", "weight": 3.0, "tags": ["any"]}
