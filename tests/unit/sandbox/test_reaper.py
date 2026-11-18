from datetime import UTC, datetime, timedelta
from uuid import UUID

from swfactory.sandbox.reaper import Labelled, select_orphans

NOW = datetime(2026, 6, 1, tzinfo=UTC)
H = timedelta(hours=1)
J1 = UUID("01890000-0000-7000-8000-000000000001")


def _l(job, exp, eph=None):
    d = {
        "factory.managed": "true",
        "factory.job_id": str(job),
        "factory.expires": str(int(exp.timestamp())),
    }
    if eph:
        d["factory.ephemeral"] = "true"
    return d


def c(j, exp=NOW + 24 * H):
    return Labelled("container", f"factory-sandbox-{j}", _l(j, exp))


def n(j, exp=NOW + 24 * H):
    return Labelled("network", f"factory-net-{j}", _l(j, exp))


def v(j, exp=NOW + 24 * H):
    return Labelled("volume", f"factory-job-{j}", _l(j, exp))


def vh(j, exp=NOW + 24 * H):
    return Labelled("volume", f"factory-job-{j}-home", _l(j, exp))


def ce(j, name, exp):
    return Labelled("container", f"factory-sandbox-{j}-{name}", _l(j, exp, True))


def ve(j, name, exp):
    return Labelled("volume", f"factory-job-{j}-{name}", _l(j, exp, True))


def names(rs):
    return [r.name for r in rs]


def test_job_not_live_is_reaped():
    got = select_orphans([v(J1), n(J1), vh(J1), c(J1)], live=set(), active=set(), now=NOW)
    assert names(got) == [
        f"factory-sandbox-{J1}",
        f"factory-net-{J1}",
        f"factory-job-{J1}",
        f"factory-job-{J1}-home",
    ]


def test_live_waiting_job_kept_until_expiry():
    assert select_orphans([c(J1, NOW + H)], live={J1}, active=set(), now=NOW) == []


def test_expired_waiting_job_loses_container_keeps_volumes_and_network():
    old = NOW - H
    got = select_orphans([c(J1, old), n(J1, old), v(J1, old), vh(J1, old)], {J1}, set(), NOW)
    assert names(got) == [f"factory-sandbox-{J1}"]


def test_active_job_never_reaped():
    assert select_orphans([c(J1, NOW - H)], live={J1}, active={J1}, now=NOW) == []


def test_ephemeral_reaped_when_job_not_active_or_expired():
    a = [ce(J1, "gate-r1-a1", NOW + H), ve(J1, "gate-r1-a1", NOW + H)]
    assert len(select_orphans(a, live={J1}, active=set(), now=NOW)) == 2
    assert len(select_orphans([ce(J1, "review-ux-r1", NOW - H)], {J1}, {J1}, NOW)) == 1
    assert select_orphans([ce(J1, "gate-r1-a1", NOW + H)], {J1}, {J1}, NOW) == []


def test_unlabelled_or_bad_label_ignored():
    bad = [
        Labelled("container", "x", {"factory.managed": "true"}),
        Labelled("container", "y", {"factory.job_id": "nope", "factory.expires": "1"}),
        Labelled("container", "z", {"factory.job_id": str(J1), "factory.expires": "soon"}),
    ]
    assert select_orphans(bad, live=set(), active=set(), now=NOW) == []
