"""The deploy config must be accepted by the tools that load it.

A Caddyfile Caddy rejects doesn't fail quietly: setup.sh reloads, the
reload fails, and the site goes down. Nothing in the app tests notices.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
CADDYFILE = DEPLOY / "Caddyfile"

# Subdirectives that only mean something inside a reverse_proxy block. At
# site level Caddy refuses the whole file ("unrecognized directive").
REVERSE_PROXY_ONLY = ("header_up", "header_down", "lb_policy", "health_uri",
                      "transport", "flush_interval")


def _site_level_lines(text: str) -> list[str]:
    """Directive lines at depth 1, i.e. directly inside the site block."""
    lines, depth = [], 0
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if depth == 1 and not line.startswith("}"):
            lines.append(line)
        depth += line.count("{") - line.count("}")
    return lines


def test_no_reverse_proxy_subdirective_at_site_level():
    directives = [re.split(r"\s", l, 1)[0] for l in _site_level_lines(CADDYFILE.read_text())]
    misplaced = [d for d in directives if d in REVERSE_PROXY_ONLY]
    assert not misplaced, f"belongs inside reverse_proxy {{ }}: {misplaced}"
    assert "reverse_proxy" in directives


@pytest.mark.skipif(shutil.which("caddy") is None, reason="caddy not installed")
def test_caddy_accepts_the_caddyfile(tmp_path):
    # Point the access log somewhere writable so only the syntax is judged.
    config = tmp_path / "Caddyfile"
    config.write_text(CADDYFILE.read_text().replace(
        "/var/log/caddy/golf.log", str(tmp_path / "golf.log")))
    result = subprocess.run(
        ["caddy", "validate", "--config", str(config), "--adapter", "caddyfile"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr[-500:]


def test_setup_script_parses():
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    result = subprocess.run([bash, "-n", str(DEPLOY / "setup.sh")],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
