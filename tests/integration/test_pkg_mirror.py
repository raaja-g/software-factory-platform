"""Needs the compose stack's pkg-mirror services plus a test-only Verdaccio `private-npm` serving
@acme-test/pkg behind a token (registered in the test definition as upstream token, credential
REGISTRY_ACME_TEST_TOKEN). Opt-in: set FACTORY_TEST_PKG_MIRROR=1 with that stack running."""

import asyncio
import os
import uuid

import docker
import pytest

from swfactory.sandbox.base import RunnerSpec
from swfactory.sandbox.docker import DockerSandboxProvider

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(
        not os.environ.get("FACTORY_TEST_PKG_MIRROR"),
        reason="opt-in: needs the pkg-mirror compose stack and the private-npm stand-in "
        "(set FACTORY_TEST_PKG_MIRROR=1)",
    ),
]
STD_DEFAULT = RunnerSpec("standard", "swfactory/runner:0.1.0", 2.0, 4096, 512, ())


@pytest.fixture
def provider(tmp_path):
    return DockerSandboxProvider(
        lambda: docker.from_env(),
        attach=("gateway-edge", "pkg-mirror"),
        spool_dir=tmp_path / "spool",
    )


@pytest.fixture
def JOB(provider):  # noqa: N802
    jid = uuid.uuid4()
    yield jid
    asyncio.run(provider.destroy(jid))


async def test_installs_go_through_the_mirror(provider, JOB):
    box = await provider.ensure(JOB, STD_DEFAULT)
    r = await box.exec(["npm", "view", "left-pad", "version"], cwd="/work")
    assert r.stdout.strip() == "1.3.0"
    r = await box.exec(["uv", "pip", "install", "--target", "/tmp/t", "six==1.17.0"], cwd="/work")
    assert r.exit_code == 0


async def test_private_scope_installs(provider, JOB):
    box = await provider.ensure(JOB, STD_DEFAULT)
    r = await box.exec(["npm", "view", "@acme-test/pkg", "version"], cwd="/work")
    assert r.stdout.strip() == "1.0.0"


async def test_publish_fails(provider, JOB):
    box = await provider.ensure(JOB, STD_DEFAULT)
    await box.write_text("/work/p/package.json", '{"name": "@acme-test/leak", "version": "1.0.0"}')
    assert (await box.exec(["npm", "publish"], cwd="/work/p")).exit_code != 0
    for method, url in (
        ("PUT", "http://pkg-mirror:4873/@acme-test%2fleak"),
        ("DELETE", "http://pkg-mirror:4873/@acme-test%2fpkg/-rev/1"),
        ("POST", "http://pkg-mirror:3141/root/pypi/"),
    ):
        r = await box.exec(
            [
                "curl",
                "-s",
                "-o",
                "/dev/null",
                "-w",
                "%{http_code}",
                "-X",
                method,
                "--data",
                "{}",
                url,
            ]
        )
        assert r.stdout in ("401", "403", "404", "405"), (method, r.stdout)


async def test_private_name_never_resolves_publicly(provider, JOB):
    box = await provider.ensure(JOB, STD_DEFAULT)
    r = await box.exec(["npm", "view", "@acme-test/only-on-npmjs", "version"], cwd="/work")
    assert r.exit_code != 0 and "404" in (r.stdout + r.stderr)


async def test_sandbox_env_has_no_credentials(provider, JOB):
    box = await provider.ensure(JOB, STD_DEFAULT)
    env = (await box.exec(["env"])).stdout
    assert "ya29" not in env and "REGISTRY_" not in env and "pkg-mirror" in env
