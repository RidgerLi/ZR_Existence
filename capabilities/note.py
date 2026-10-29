"""示例能力：往本地便签里追加一行。

参数：{"text": "要记的内容"}
"""

import os
from datetime import datetime

NAME = "note"
DESCRIPTION = "把一段文字追加到 resources/memory/notes.md（本地纯文本便签）。"

_PATH = os.path.join("resources", "memory", "notes.md")


def run(args: dict) -> str:
    text = str((args or {}).get("text", "")).strip()
    if not text:
        raise ValueError("args.text is required")
    os.makedirs(os.path.dirname(_PATH), exist_ok=True)
    with open(_PATH, "a", encoding="utf-8") as f:
        f.write(f"- [{datetime.now().strftime('%Y-%m-%d %H:%M')}] {text}\n")
    return f"已记录到 {_PATH}"
