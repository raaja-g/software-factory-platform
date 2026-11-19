from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from tests.unit.definition.test_models import defaults_data

from swfactory.automations.scheduler import Scheduler, due_fire
from swfactory.definition.models import AutomationDef


def test_due_fire_utc_no_backfill():
    now = datetime(2026, 11, 11, 9, 7, tzinfo=UTC)
    assert due_fire("0 9 * * *", last=None, now=now) == datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
    assert due_fire("0 9 * * *", last=datetime(2026, 11, 11, 9, 0, tzinfo=UTC), now=now) is None
    assert due_fire(
        "0 * * * *", last=datetime(2026, 11, 10, 0, 0, tzinfo=UTC), now=now
    ) == datetime(2026, 11, 11, 9, 0, tzinfo=UTC)  # latest only


def test_due_fire_on_the_slot_and_alias():
    now = datetime(2026, 11, 11, 9, 0, tzinfo=UTC)
    assert due_fire("0 9 * * *", last=None, now=now) == now
    assert due_fire("@daily", last=None, now=now) == datetime(2026, 11, 11, 0, 0, tzinfo=UTC)


def automation(trigger: dict, **extra):
    from swfactory.definition.models import Definition

    data = defaults_data()
    data["webhooks"] = {
        "gh": {
            "auth_mode": "signature",
            "signature_scheme": "github",
            "secret_env": "WEBHOOK_GH_SECRET",
            "delivery_id_header": "X-GitHub-Delivery",
        }
    }
    data["automations"] = {
        "a": {"trigger": trigger, "app": "demo", "feature_template": "do it", **extra}
    }
    return Definition.model_validate(data).automations["a"]


def test_trigger_shapes():
    ok = automation(
        {"provider": "schedule", "event": "cron_fired", "filter": {"cron": "0 3 * * 1"}}
    )
    assert ok.trigger.filter["cron"] == "0 3 * * 1" and ok.max_open_jobs == 1
    assert automation({"provider": "schedule", "event": "cron_fired", "filter": {"cron": "@daily"}})
    with pytest.raises(ValidationError, match="filter.cron"):
        automation({"provider": "schedule", "event": "cron_fired", "filter": {}})
    with pytest.raises(ValidationError, match="filter.cron"):
        automation(
            {"provider": "schedule", "event": "cron_fired", "filter": {"cron": "61 * * * *"}}
        )
    with pytest.raises(ValidationError, match="cron_fired"):
        automation({"provider": "schedule", "event": "x", "filter": {"cron": "0 3 * * 1"}})
    with pytest.raises(ValidationError, match="webhook_ids"):
        automation(
            {"provider": "webhook", "event": "received", "filter": {"webhook_ids": ["nope"]}}
        )
    with pytest.raises(ValidationError, match="webhook_ids"):
        automation({"provider": "webhook", "event": "received", "filter": {}})
    assert automation(
        {"provider": "webhook", "event": "received", "filter": {"webhook_ids": ["gh"]}}
    )


def test_cap_fields_validated():
    sched = {"provider": "schedule", "event": "cron_fired", "filter": {"cron": "0 3 * * 1"}}
    with pytest.raises(ValidationError, match="max_open_jobs"):
        automation(sched, max_open_jobs=0)
    with pytest.raises(ValidationError, match="min_interval_s"):
        automation(sched, min_interval_s=-1)
    assert automation(sched, min_interval_s=60).min_interval_s == 60


class _Defs:
    def __init__(self, autos):
        self.autos = autos

    async def active(self):
        class D:
            automations = self.autos

        return "sha", D()


class _NoSession:
    def begin(self):
        raise AssertionError("no database in this test")


async def test_disabled_automation_never_fires():
    auto = AutomationDef.model_validate(
        {
            "enabled": False,
            "trigger": {
                "provider": "schedule",
                "event": "cron_fired",
                "filter": {"cron": "* * * * *"},
            },
            "app": "demo",
            "feature_template": "x",
        }
    )
    sched = Scheduler(_NoSession(), _Defs({"n": auto}))
    assert await sched.tick(datetime(2026, 11, 11, 9, 7, tzinfo=UTC)) == []
