"""Run each widget in jsdom against a stub MCP Apps host (npm install in tests/widgets first)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from gateway.mcp import ui

HARNESS = Path(__file__).parent / "widgets"

pytestmark = pytest.mark.skipif(
    not (shutil.which("node") and (HARNESS / "node_modules" / "jsdom").exists()),
    reason="install jsdom: cd tests/widgets && npm install",
)

FILES = {"ui://tasks/list": "tasks"}


@pytest.mark.parametrize("uri", sorted(ui.WIDGETS))
def test_widget_behaviour(uri, tmp_path):
    name = FILES[uri]
    (tmp_path / f"{name}.html").write_text(ui.render_widget(uri), encoding="utf-8")
    done = subprocess.run(
        ["node", str(HARNESS / f"{name}.test.mjs"), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
