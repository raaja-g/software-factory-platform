import pytest

from swfactory.domain.types import HumanAction
from swfactory.github.commands import ParsedCommand, comment_payload, parse_command


@pytest.mark.parametrize(
    "body,expected",
    [
        ("/factory approve", ParsedCommand(HumanAction.APPROVE, None)),
        (
            "Looks good.\n/factory rework add a timeout to the socket",
            ParsedCommand(HumanAction.REWORK, "add a timeout to the socket"),
        ),
        ("/factory reject", ParsedCommand(HumanAction.REJECT, None)),
        ("```\n/factory approve\n```", None),
        ("~~~\n/factory approve\n~~~", None),
        ("    /factory approve", None),
        ("> /factory approve", None),
        ("please `/factory approve`", None),
        ("/factory rework", None),
        ("/factory approved", None),
        ("/factory retry", None),
    ],
)
def test_parse_command(body, expected):
    assert parse_command(body) == expected


def test_comment_payload_shape():
    from datetime import UTC, datetime

    from swfactory.github.client import IssueComment

    c = IssueComment(
        9, "ana", "User", "/factory approve", datetime(2026, 1, 1, tzinfo=UTC), None, 1
    )
    p = comment_payload(c, repo="acme/web", pr_url="u")
    assert p["action"] == "created" and p["sender"] == {"login": "ana", "type": "User"}
    assert p["issue"]["pull_request"]["html_url"] == "u"
