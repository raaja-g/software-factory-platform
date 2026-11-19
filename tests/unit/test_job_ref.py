import inspect
from uuid import uuid4

import pytest

from swfactory.commands import format_job_ref, parse_job_ref, submit_command


@pytest.mark.parametrize("ref", ["todo:003", "todo:3", "todo/3", "todo#003"])
def test_parse_job_ref_forms(ref):
    assert parse_job_ref(ref) == ("todo", 3)


def test_parse_job_ref_uuid():
    u = uuid4()
    assert parse_job_ref(str(u)) == u


@pytest.mark.parametrize("ref", ["", "todo", "todo:x", ":3", "todo:3:4"])
def test_parse_job_ref_rejects(ref):
    with pytest.raises(ValueError):
        parse_job_ref(ref)


def test_format_job_ref():
    assert format_job_ref("todo", 1) == "todo:001"


def test_submit_command_signature():
    assert list(inspect.signature(submit_command).parameters) == [
        "s", "job_id", "action", "note", "requested_by", "head_sha", "source_ref", "amount_usd",
    ]  # fmt: skip
