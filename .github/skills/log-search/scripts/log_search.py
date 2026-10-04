#!/usr/bin/env python3
"""log-digest の出力を DuckDB で検索する（障害ログAI調査ガイド 案2）。使い方は SKILL.md を参照。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from logsearch.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
