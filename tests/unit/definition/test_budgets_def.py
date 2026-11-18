from decimal import Decimal

import pytest
from pydantic import ValidationError
from tests.unit.definition.test_models import defaults_data, set_in

from swfactory.definition.models import Budgets, validate_definition


def _data(**budgets):
    data = defaults_data()
    app = next(iter(data["factory"]["apps"]))
    data["factory"]["budgets"] = {"app_monthly_usd": {app: "200"}, **budgets}
    return data, app


def test_app_monthly_def():
    data, app = _data()
    defn, issues = validate_definition(data)
    assert not issues and defn is not None
    assert defn.factory.budgets.app_monthly_usd == {app: Decimal("200")}
    bad = set_in(data, ("factory", "budgets", "app_monthly_usd"), {"nope": "5"})
    _, issues = validate_definition(bad)
    assert any("app_monthly_usd: unknown app nope" in i.msg for i in issues)


def test_step_max_defaults_and_bounds():
    b = Budgets()
    assert (b.step_max_usd["builder"], b.step_max_usd["reviewer-code"]) == (
        Decimal("5"),
        Decimal("1"),
    )
    assert b.step_max_usd["approver"] == Decimal("0.5")
    b = Budgets(step_max_usd={"builder": "8"})  # type: ignore[arg-type]
    assert b.step_max_usd["builder"] == Decimal("8")
    assert b.step_max_usd["judge"] == Decimal("0.25")  # unnamed roles keep their default
    with pytest.raises(
        ValidationError, match="step_max_usd: builder 0.10 is below its step minimum 0.50"
    ):
        Budgets(step_max_usd={"builder": "0.10"})  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="step_max_usd: unknown role nope"):
        Budgets(step_max_usd={"nope": "1"})  # type: ignore[arg-type]


def test_step_max_through_definition():
    data, _ = _data(step_max_usd={"builder": "8"})
    defn, issues = validate_definition(data)
    assert not issues and defn is not None
    assert defn.factory.budgets.step_max_usd["builder"] == Decimal("8")
