"""The prepare-commit-msg hook that adds Factory trailers to builder commits."""

from __future__ import annotations

from swfactory.sandbox.base import Sandbox
from swfactory.sandbox.paths import REPO

HOOKS_DIR = "/work/.factory/git/hooks"
HOOK_PATH = f"{HOOKS_DIR}/prepare-commit-msg"

# Reads only FACTORY_JOB, FACTORY_ROLE and FACTORY_MODEL; no secret is involved.
PREPARE_COMMIT_MSG = """#!/bin/sh
# Adds Factory-Job, Factory-Role and Factory-Model trailers when all three are set.
if [ -z "${FACTORY_JOB:-}" ] || [ -z "${FACTORY_ROLE:-}" ] || [ -z "${FACTORY_MODEL:-}" ]; then
  exit 0
fi
git interpret-trailers --in-place --if-exists replace \\
  --trailer "Factory-Job: $FACTORY_JOB" \\
  --trailer "Factory-Role: $FACTORY_ROLE" \\
  --trailer "Factory-Model: $FACTORY_MODEL" "$1"
"""


async def install_trailer_hook(box: Sandbox) -> None:
    await box.write_text(HOOK_PATH, PREPARE_COMMIT_MSG)
    res = await box.exec(["chmod", "0755", HOOK_PATH], cwd=REPO)
    if res.exit_code != 0:
        raise RuntimeError(f"cannot make the commit hook executable: {res.stderr.strip()}")
    res = await box.exec(["git", "-C", REPO, "config", "core.hooksPath", HOOKS_DIR], cwd=REPO)
    if res.exit_code != 0:
        raise RuntimeError(f"cannot set core.hooksPath: {res.stderr.strip()}")
