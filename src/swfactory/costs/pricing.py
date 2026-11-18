"""Cost estimates and the per-role step minimums."""

from collections.abc import Mapping
from decimal import Decimal

from swfactory.domain.types import Role, Usage
from swfactory.models.catalog import ModelInfo

_MILLION = Decimal(1_000_000)
_SIX = Decimal("0.000001")

STEP_MIN_USD: Mapping[Role, Decimal] = {
    Role.TRIAGE: Decimal("0.01"),
    Role.REQUIREMENTS: Decimal("0.20"),
    Role.SPEC_WRITER: Decimal("0.10"),
    Role.BUILDER: Decimal("0.50"),
    Role.REVIEWER_SECURITY: Decimal("0.10"),
    Role.REVIEWER_UX: Decimal("0.10"),
    Role.REVIEWER_UI: Decimal("0.10"),
    Role.REVIEWER_CODE: Decimal("0.10"),
    Role.APPROVER: Decimal("0.05"),
    Role.JUDGE: Decimal("0.02"),
    Role.IMPROVER: Decimal("0.50"),
}

STEP_MAX_DEFAULT_USD: Mapping[Role, Decimal] = {
    Role.TRIAGE: Decimal("0.25"),
    Role.REQUIREMENTS: Decimal("2"),
    Role.SPEC_WRITER: Decimal("1"),
    Role.BUILDER: Decimal("5"),
    Role.REVIEWER_SECURITY: Decimal("1"),
    Role.REVIEWER_UX: Decimal("1"),
    Role.REVIEWER_UI: Decimal("1"),
    Role.REVIEWER_CODE: Decimal("1"),
    Role.APPROVER: Decimal("0.5"),
    Role.JUDGE: Decimal("0.25"),
    Role.IMPROVER: Decimal("5"),
}


def estimate_cost(info: ModelInfo, usage: Usage) -> Decimal:
    total = (usage.input_tokens + usage.cache_read_tokens) * info.price_in_per_mtok + (
        usage.output_tokens * info.price_out_per_mtok
    )
    return (total / _MILLION).quantize(_SIX)
