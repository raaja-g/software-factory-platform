from decimal import Decimal
from uuid import uuid4

import pytest
from tests.fakes.stub_sandbox import StubSandbox

from swfactory.domain.types import AgentTask, ModelRef, Role, ToolPolicy
from swfactory.settings import Settings


@pytest.fixture
def stub_box(tmp_path):
    return StubSandbox(tmp_path, None)


@pytest.fixture
def settings():
    return Settings()


@pytest.fixture
def task():
    run_id = uuid4()
    return AgentTask(
        job_id=uuid4(),
        run_id=run_id,
        role=Role.BUILDER,
        round=1,
        attempt=1,
        engine="fake",
        model=ModelRef("sonnet@vertex", "vertex", "claude-sonnet-4-6"),
        system_prompt="sys",
        prompt="do it",
        workdir="/work/repo",
        output_path=f"/work/.factory/out/{run_id}.md",
        tool_policy=ToolPolicy.WRITE,
        attachments=(),
        max_turns=10,
        timeout_s=60,
        resume_session=None,
        gateway_base_url="http://gateway:4000",
        gateway_key="sk-run-secret-123",
        tags={},
        max_budget_usd=Decimal("1"),
    )
