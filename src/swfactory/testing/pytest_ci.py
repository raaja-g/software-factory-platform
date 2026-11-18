"""Under FACTORY_CI=1 a skipped test fails unless its reason starts with ``opt-in:``."""

import os

import pytest

OPT_IN = "opt-in:"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if os.environ.get("FACTORY_CI") != "1" or not report.skipped:
        return report
    if hasattr(report, "wasxfail"):
        return report
    longrepr = report.longrepr
    reason = longrepr[2] if isinstance(longrepr, tuple) else str(longrepr)
    reason = reason.removeprefix("Skipped: ")
    if not reason.startswith(OPT_IN):
        report.outcome = "failed"
        report.longrepr = f"skipped under FACTORY_CI=1: {reason}"
    return report
