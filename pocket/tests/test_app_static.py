"""The phone app's own rules (S4, S5): text-only rendering, no inline code,
no secrets, and its pure helpers pass under node."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
HERE = Path(__file__).resolve().parent


def test_no_html_injection_sinks_in_the_app():
    sinks = re.compile(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|"
                       r"setTimeout\(\s*['\"]|srcdoc|createContextualFragment|DOMParser")
    for f in STATIC.glob("*.js"):
        hits = [line for line in f.read_text().splitlines() if sinks.search(line)]
        assert not hits, f"{f.name}: {hits}"


def test_index_has_no_inline_script_style_or_handlers():
    html = (STATIC / "index.html").read_text()
    assert not re.search(r"<script(?![^>]*\bsrc=)", html), "inline <script>"
    assert "<style" not in html and "style=" not in html
    assert not re.search(r"\son[a-z]+\s*=", html), "inline event handler"
    assert not re.search(r"(src|href)=\"(https?:)?//", html), "off-origin asset"


def test_app_loads_nothing_off_origin():
    for f in list(STATIC.glob("*.js")) + list(STATIC.glob("*.css")):
        assert not re.search(r"https?://", f.read_text()), f.name


def test_service_worker_never_caches_the_api():
    sw = (STATIC / "sw.js").read_text()
    assert "startsWith('/app/')" in sw and "/api" not in sw.split("const SHELL")[1].split(";")[0]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_app_helpers_under_node():
    r = subprocess.run(["node", "--test", str(HERE / "app_helpers.test.cjs")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
