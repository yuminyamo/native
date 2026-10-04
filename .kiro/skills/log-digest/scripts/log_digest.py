#!/usr/bin/env python3
"""障害ログ一式から、AIが最初に読むダイジェストを作る。使い方は SKILL.md を参照。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from logdigest.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
