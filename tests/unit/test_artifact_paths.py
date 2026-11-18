import pytest

from swfactory.artifacts import validate_artifact_path


def test_validate_artifact_path():
    assert validate_artifact_path("round-1/review-ui.md") == "round-1/review-ui.md"
    for bad in ("../x", "/etc/passwd", "a/../../b", ""):
        with pytest.raises(ValueError):
            validate_artifact_path(bad)
