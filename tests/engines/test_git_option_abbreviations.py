"""git accepts abbreviated long options; the read-only deny rules must catch every spelling."""

import fnmatch

import pytest

from swfactory.engines.opencode import _GIT_READ

ABBREVIATIONS = [
    "git diff --output=/tmp/x", "git diff --outp=/tmp/x", "git diff --ou=/tmp/x",
    "git log --ext-diff", "git log --ext", "git show --ext-d",
    "git diff --open-files-in-pager=sh", "git diff --open=sh",
    "git diff a\nrm -rf /", "git diff a; rm x", "git diff a && curl x", "git diff > f",
]  # fmt: skip
FINE = [
    "git diff",
    "git diff HEAD~1",
    "git log --oneline -5",
    "git show HEAD:README.md",
    "git status",
]


def _decision(command: str) -> str:
    verdict = "deny"
    for pattern, decision in _GIT_READ.items():  # the last matching rule wins
        if fnmatch.fnmatchcase(command, pattern):
            verdict = decision
    return verdict


@pytest.mark.parametrize("command", ABBREVIATIONS)
def test_unsafe_git_spellings_are_denied(command):
    assert _decision(command) == "deny"


@pytest.mark.parametrize("command", FINE)
def test_ordinary_read_only_git_is_allowed(command):
    assert _decision(command) == "allow"
