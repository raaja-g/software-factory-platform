import pytest
from tests.fakes.github_api import FakeGhApi, StubAuth, mock_github

from swfactory.apps import GitHubRepoInspector, RepoInfo


@pytest.fixture
def fake_gh():
    api = FakeGhApi()
    with mock_github(api):
        yield api


async def test_inspector_reads_the_default_branch(fake_gh):
    auth = StubAuth()
    assert await GitHubRepoInspector(auth).inspect("acme/web") == RepoInfo(True, "trunk", False)
    fake_gh.installed = False
    assert await GitHubRepoInspector(auth).inspect("acme/web") == RepoInfo(False, None, False)


async def test_inspector_flags_an_empty_repo(fake_gh):
    fake_gh.empty = True
    assert await GitHubRepoInspector(StubAuth()).inspect("acme/web") == RepoInfo(
        True, "trunk", True
    )


async def test_inspector_only_reads(fake_gh):
    await GitHubRepoInspector(StubAuth()).inspect("acme/web")
    assert {m for m, _ in fake_gh.calls} == {"GET"}
