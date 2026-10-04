#!/usr/bin/env python3
"""skills/ 直下の各skillを、GitHub Copilot と Kiro が読むディレクトリへコピーする。

  skills/<name>/  →  .github/skills/<name>/   (GitHub Copilot)
                  →  .kiro/skills/<name>/     (Kiro)

正本は skills/ で、コピー先は直接編集しない。skills/ を変更したら手動で実行し、
コピー結果もコミットする。

  python tools/sync_skills.py                # 両方へコピー
  python tools/sync_skills.py --target kiro  # 片方だけ
  python tools/sync_skills.py --check        # コピー先が最新か確認するだけ（差分があれば終了コード 1）

コピー先にある、skills/ に存在しない別のskillには触らない。
標準ライブラリだけで動く。
"""

from __future__ import annotations

import argparse
import filecmp
import fnmatch
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, List

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "skills"
TARGETS: Dict[str, Path] = {
    "github": REPO / ".github" / "skills",
    "kiro": REPO / ".kiro" / "skills",
}
IGNORE = ["__pycache__", "*.pyc", ".venv", ".DS_Store", ".pytest_cache", "log-digest-out"]

_FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n", re.DOTALL)


def _ignored(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in IGNORE)


def find_skills() -> List[Path]:
    return sorted(p for p in SOURCE.iterdir() if p.is_dir() and (p / "SKILL.md").is_file())


def validate(skill: Path) -> List[str]:
    """SKILL.md の frontmatter が Copilot / Kiro 共通の形（name, description）になっているか。"""
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return [f"{skill.name}: SKILL.md の先頭に YAML frontmatter（--- で囲む）がありません"]
    fields = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and not line.startswith((" ", "\t")):
            fields[key.strip()] = value.strip()
    errors = []
    name = fields.get("name", "")
    if name != skill.name:
        errors.append(f"{skill.name}: frontmatter の name（{name!r}）をディレクトリ名と同じにしてください")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skill.name) or len(skill.name) > 64:
        errors.append(f"{skill.name}: skill 名は英小文字・数字・ハイフンで64文字以内にしてください")
    desc = fields.get("description", "")
    if not desc:
        errors.append(f"{skill.name}: frontmatter に description がありません")
    elif len(desc) > 1024:
        errors.append(f"{skill.name}: description が {len(desc)} 文字あります（1024文字以内）")
    return errors


def list_files(root: Path) -> List[str]:
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(_ignored(part) for part in rel.parts):
            continue
        if p.is_file():
            out.append(rel.as_posix())
    return out


def diff(src: Path, dest: Path) -> List[str]:
    if not dest.is_dir():
        return [f"{dest.relative_to(REPO)} がありません"]
    a, b = set(list_files(src)), set(list_files(dest))
    problems = [f"{dest.relative_to(REPO)}/{f} がありません" for f in sorted(a - b)]
    problems += [f"{dest.relative_to(REPO)}/{f} は skills/ にありません" for f in sorted(b - a)]
    problems += [f"{dest.relative_to(REPO)}/{f} の内容が異なります"
                 for f in sorted(a & b) if not filecmp.cmp(src / f, dest / f, shallow=False)]
    return problems


def copy(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=lambda _d, names: [n for n in names if _ignored(n)])


def main(argv: List[str] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--target", choices=sorted(TARGETS) + ["all"], default="all")
    p.add_argument("--check", action="store_true", help="コピーせず、コピー先が最新か確認する")
    args = p.parse_args(argv)

    skills = find_skills()
    if not skills:
        print("skills/ に SKILL.md を持つディレクトリがありません", file=sys.stderr)
        return 1
    errors = [e for s in skills for e in validate(s)]
    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        return 1

    targets = sorted(TARGETS) if args.target == "all" else [args.target]
    problems: List[str] = []
    for skill in skills:
        for t in targets:
            dest = TARGETS[t] / skill.name
            if args.check:
                problems += diff(skill, dest)
            else:
                copy(skill, dest)
                print(f"{skill.relative_to(REPO)} -> {dest.relative_to(REPO)}")
    if args.check:
        if problems:
            for msg in problems:
                print(f"out of date: {msg}")
            print("python tools/sync_skills.py を実行してください", file=sys.stderr)
            return 1
        print("up to date")
    return 0


if __name__ == "__main__":
    sys.exit(main())
