import json
import re
from pathlib import Path


def test_playwright_is_lockfile_pinned():
    pkg = json.loads(Path("docker/runner-web/package.json").read_text())
    lock = json.loads(Path("docker/runner-web/package-lock.json").read_text())
    assert re.fullmatch(r"\d+\.\d+\.\d+", pkg["dependencies"]["playwright"])
    assert (
        lock["packages"]["node_modules/playwright"]["version"] == pkg["dependencies"]["playwright"]
    )
    df = Path("docker/runner-web.Dockerfile").read_text()
    assert "npm ci --omit=dev" in df and "npm install -g" not in df
