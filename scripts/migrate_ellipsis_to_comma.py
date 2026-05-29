"""One-off migration: restore the pause that ellipsis used to encode.

Earlier the cleaner deleted `…` / `……` outright; we now want them as `，`.
Because the ellipsis position is no longer in the on-disk filename, we
hard-code the originals (only the 5 files that actually contained `…`)
and re-derive both the previous on-disk name and the desired name.
"""

from __future__ import annotations

import os
import sys

from clean_tts_prompt_filenames import TAG_RE, CHARS_TO_REMOVE, ELLIPSES, clean_transcript

DIR = r"C:\Work\ai\ZerolanLiveRobot\resources\static\prompts\tts"

ORIGINALS = [
    "[zh][平常]这世界像个马戏团，昼夜不停地上演着禽兽相争的滑稽戏：狗熊骑独轮车、狮子钻火圈、猴子顶碗…而我们「愚者」和其他人的区别在于，我们知道自己在表演~.wav",
    "[zh][思索]咦？奇怪…我明明按了按钮呀，是哪里出了问题…….wav",
    "[zh][放松]列车的沙发实在太柔软了，睡着之后…连梦都是柔软的。.wav",
    "[zh][玩味]优雅、有趣、高贵，但是危险…越是这样的人，越是想摧毁她，不过我也可以退而求其次，被她摧毁…不是不行！.wav",
    "[zh][轻蔑]全宇宙有哪个不知道你们茨冈尼亚人？天生的骗子、小偷、交际花…口蜜腹剑，名副其实。.wav",
]


def old_clean(text: str) -> str:
    """Reproduce the previous policy: drop ellipsis entirely."""
    cleaned = text
    for token in ELLIPSES:
        cleaned = cleaned.replace(token, "")
    cleaned = "".join(ch for ch in cleaned if ch not in CHARS_TO_REMOVE)
    return cleaned


def derive(original: str, transform) -> str:
    m = TAG_RE.match(original)
    assert m, f"unexpected filename: {original}"
    prefix, transcript, ext = m.group(1), m.group(2), m.group(3)
    return f"{prefix}{transform(transcript)}{ext}"


def main(apply: bool) -> int:
    plans: list[tuple[str, str]] = []
    for original in ORIGINALS:
        current = derive(original, old_clean)
        desired = derive(original, clean_transcript)
        if current == desired:
            continue
        plans.append((current, desired))

    if not plans:
        print("nothing to migrate.")
        return 0

    for cur, des in plans:
        print(f"{cur}\n  -> {des}\n")

    if not apply:
        print(f"(dry-run) {len(plans)} file(s) would be renamed. "
              "re-run with --apply to perform.")
        return 0

    print("--- applying ---")
    for cur, des in plans:
        src = os.path.join(DIR, cur)
        dst = os.path.join(DIR, des)
        if not os.path.exists(src):
            print(f"[miss] source not found: {cur}", file=sys.stderr)
            continue
        if os.path.exists(dst):
            print(f"[skip] target already exists: {des}", file=sys.stderr)
            continue
        os.rename(src, dst)
        print(f"[ok] {cur}\n  -> {des}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main(apply="--apply" in sys.argv))
