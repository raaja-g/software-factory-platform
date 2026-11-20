from __future__ import annotations

from datetime import UTC, datetime

from swfactory.scoring.objective import CommitMessage, due_daily, find_revert

M = "1ff7785" + "0" * 33
D = datetime(2026, 3, 1, 12, tzinfo=UTC)


def msg(text: str) -> CommitMessage:
    return CommitMessage("c" * 40, text, D)


def at(hm: str) -> datetime:
    h, m = hm.split(":")
    return datetime(2026, 3, 1, int(h), int(m), tzinfo=UTC)


def test_reverts_merge_commit():
    m = msg('Revert "x"\n\nThis reverts commit 1ff7785.')
    assert find_revert([m], merge_sha=M, branch_shas=(), title="t") == m


def test_reverts_branch_commit():
    m = msg("This reverts commit e2c6ba2abc.")
    assert find_revert([m], merge_sha=M, branch_shas={"e2c6ba2abc" + "0" * 30}, title="t") == m


def test_revert_by_title():
    m = msg('Revert "Local todo web page"')
    assert find_revert([m], merge_sha=M, branch_shas=(), title="Local todo web page") == m


def test_unrelated_is_none():
    assert (
        find_revert([msg("This reverts commit abc1234.")], merge_sha=M, branch_shas=(), title="t")
        is None
    )


def test_short_prefix_ignored():
    m = msg("This reverts commit 1ff77.")
    assert find_revert([m], merge_sha="1ff77" + "0" * 35, branch_shas=(), title="t") is None


def test_due_daily():
    assert not due_daily(None, at("01:59"))
    assert due_daily(None, at("02:00"))
    assert not due_daily(at("02:05"), at("23:00"))
    assert due_daily(datetime(2026, 2, 28, 2, 5, tzinfo=UTC), at("02:00"))
