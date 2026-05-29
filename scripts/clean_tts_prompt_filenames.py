"""Clean punctuation in TTS prompt filenames.

Keeps the mandatory [lang][sentiment] tags and the .wav extension intact.
Strips decorative / bracket-like punctuation from the transcript portion
so that the text fed downstream (sentiment analysis, TTS prompt_text)
is less likely to be derailed by tokens such as 「」 ～ … etc.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

CHARS_TO_REMOVE = set(
    "「」『』（）()【】《》"  # brackets
    "～~"                      # tilde / wave dash
    "\u201c\u201d\u2018\u2019\"'"  # smart/straight quotes
)
ELLIPSES = ("……", "…")

TAG_RE = re.compile(r"^(\[[^\]]+\]\[[^\]]+\])(.*)(\.wav)$", re.IGNORECASE)


def clean_transcript(text: str) -> str:
    cleaned = text
    # Ellipses convey a real beat/pause; preserve it as a comma instead of dropping.
    for token in ELLIPSES:
        cleaned = cleaned.replace(token, "，")
    cleaned = "".join(ch for ch in cleaned if ch not in CHARS_TO_REMOVE)
    cleaned = re.sub(r"，{2,}", "，", cleaned)
    cleaned = cleaned.rstrip("，")
    return cleaned


def plan_renames(directory: str) -> list[tuple[str, str]]:
    plans: list[tuple[str, str]] = []
    for name in sorted(os.listdir(directory)):
        if not name.lower().endswith(".wav"):
            continue
        m = TAG_RE.match(name)
        if not m:
            print(f"[skip] no [lang][sentiment] tags: {name}", file=sys.stderr)
            continue
        prefix, transcript, ext = m.group(1), m.group(2), m.group(3)
        new_transcript = clean_transcript(transcript)
        new_name = f"{prefix}{new_transcript}{ext}"
        if new_name != name:
            plans.append((name, new_name))
    return plans


def apply_renames(directory: str, plans: list[tuple[str, str]]) -> None:
    for old, new in plans:
        src = os.path.join(directory, old)
        dst = os.path.join(directory, new)
        if os.path.exists(dst):
            print(f"[conflict] target already exists, skip: {new}", file=sys.stderr)
            continue
        os.rename(src, dst)
        print(f"[ok] {old}\n  -> {new}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory")
    parser.add_argument("--apply", action="store_true", help="actually perform the rename")
    args = parser.parse_args()

    if not os.path.isdir(args.directory):
        print(f"directory not found: {args.directory}", file=sys.stderr)
        return 1

    plans = plan_renames(args.directory)
    if not plans:
        print("nothing to rename.")
        return 0

    for old, new in plans:
        print(f"{old}\n  -> {new}\n")

    if args.apply:
        print("--- applying ---")
        apply_renames(args.directory, plans)
    else:
        print(f"(dry-run) {len(plans)} file(s) would be renamed. "
              f"re-run with --apply to perform.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    raise SystemExit(main())
