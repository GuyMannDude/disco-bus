"""robot.info drift guard (Sparks convention, 2026-07-05).

Disco-Bus has no package version; the CHANGELOG's newest release heading
is the version anchor. This fails the suite the moment robot.info drifts from
it, so a release can't ship with a stale manifest. Parsing per
ROBOT-INFO-SPEC: strip // line comments, then standard JSON.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_robot_info() -> dict:
    raw = (ROOT / "robot.info").read_text(encoding="utf-8")
    return json.loads(re.sub(r"^\s*//.*$", "", raw, flags=re.M))


def _changelog_version() -> str:
    raw = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    m = re.search(r"^## v?(\d+\.\d+[^\s]*) ", raw, flags=re.M)
    assert m, "CHANGELOG.md has no release heading (## <version> — ...)"
    return m.group(1)


def test_robot_info_parses_per_spec():
    info = _load_robot_info()
    for field in ("robot_info_version", "name", "version", "license", "source"):
        assert isinstance(info.get(field), str) and info[field], f"missing/empty {field}"


def test_robot_info_version_matches_changelog():
    info = _load_robot_info()
    expected = _changelog_version()
    assert info["version"] == expected, (
        f'robot.info says {info["version"]} but the newest CHANGELOG release is {expected} — '
        "update robot.info as part of the version bump."
    )
