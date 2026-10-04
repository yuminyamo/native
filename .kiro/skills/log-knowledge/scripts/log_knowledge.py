#!/usr/bin/env python3
"""再現環境のログから既知ノイズ辞書を作る（障害ログAI調査ガイド 案3）。使い方は SKILL.md を参照。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from logknowledge.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
