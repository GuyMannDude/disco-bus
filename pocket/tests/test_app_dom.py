"""The phone app in a real browser (spec §9): a hostile letter renders as
text, a 50 KB letter collapses behind Show more, and nothing trips the CSP.
Headless Chrome is driven over CDP by dom_check.cjs (node, no npm)."""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from conftest import LOGIN, TOKEN, Pocket

HERE = Path(__file__).resolve().parent
CHROME = shutil.which("google-chrome") or shutil.which("chromium")
XSS = '<img src=x onerror="window.__pwned=1;alert(1)">'
XSS_SUBJECT = "<script>window.__pwned=2</script>"
LONG = ("word " * 10000).strip()          # 50 KB

pytestmark = pytest.mark.skipif(CHROME is None or shutil.which("node") is None,
                                reason="needs google-chrome and node")


@pytest.fixture
def chrome(tmp_path):
    profile = tmp_path / "chrome-profile"
    proc = subprocess.Popen(
        [CHROME, "--headless=new", "--no-first-run", "--no-default-browser-check", "--disable-gpu",
         "--disable-extensions", "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    port_file = profile / "DevToolsActivePort"
    for _ in range(150):
        if port_file.exists() and len(port_file.read_text().splitlines()) >= 2:
            break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("Chrome never opened its debugging port")
    port, ws_path = port_file.read_text().splitlines()[:2]
    yield f"ws://127.0.0.1:{port}{ws_path}"
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_hostile_and_huge_letters_render_safely(tmp_path, stub, chrome):
    xss_id = stub.add("CC", "Guy", XSS_SUBJECT, {"text": XSS})
    long_id = stub.add("Opie", "Guy", "a very long letter", {"text": LONG})
    stub.add("Guy", "CC", "a note back", {"text": "hello", "via": "pocket"})
    p = Pocket(tmp_path, stub)
    shots = os.environ.get("POCKET_SCREENSHOTS")
    try:
        r = subprocess.run(["node", str(HERE / "dom_check.cjs"), chrome, p.base, TOKEN, LOGIN,
                            str(xss_id), str(long_id)] + ([shots] if shots else []),
                           capture_output=True, text=True, timeout=90)
    finally:
        p.close()
    assert r.returncode == 0, r.stderr
    f = json.loads(r.stdout)

    assert f["setupShownWithoutToken"]
    assert f["fleetCards"] == 4
    assert f["leaks"] == [], "a screen printed null/undefined"
    # The hostile letter is text, everywhere it appears.
    assert f["inboxRows"] == 2 and f["inboxImgs"] == 0 and f["inboxShowsPayloadAsText"]
    assert f["xss"]["text"] == XSS
    assert f["xss"]["subject"] == XSS_SUBJECT
    assert f["xss"]["imgs"] == 0
    assert f["xss"]["pwned"] is None and f["pwnedAtEnd"] is None
    assert f["dialogs"] == []
    # The huge letter collapses, opens fully, and closes again.
    assert f["long"]["before"] <= 1202 and f["long"]["label0"] == "Show more"
    assert f["long"]["after"] == len(LONG) and f["long"]["label1"] == "Show less"
    assert f["long"]["back"] == f["long"]["before"]
    # Nothing the app does needs an exception to the CSP, and nothing threw.
    assert f["cspViolations"] == 0
    assert f["exceptions"] == []
