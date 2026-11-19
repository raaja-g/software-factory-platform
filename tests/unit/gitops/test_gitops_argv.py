import subprocess
from uuid import UUID

from swfactory.gitops.github import (
    CREDENTIAL_HELPER,
    GITOPS_REPO,
    HARDENED_GIT_ENV,
    git_argv,
    hardened_git_args,
)
from swfactory.sandbox.template import gitops_config, gitops_volume
from swfactory.settings import FactoryMode, Settings

JOB = UUID("01890000-0000-7000-8000-000000000001")
IMG = "platform@sha256:" + "0" * 64


def test_token_never_in_argv_or_url():
    argv = git_argv("push", repo="acme/web", branch="factory/001-x", expect_sha="a" * 40)
    assert "https://github.com/acme/web.git" in argv and "x-access-token:" not in " ".join(argv)
    assert "--force-with-lease=refs/heads/factory/001-x:" + "a" * 40 in argv


def test_export_bundle_since_excludes_known_commits():
    a = git_argv("export_bundle", repo="acme/web", branch="main", since="a" * 40)
    assert a[-4:] == ["create", "/git/xfer/base.bundle", "^" + "a" * 40, "refs/heads/main"]
    b = git_argv("export_bundle", repo="acme/web", branch="main")
    assert b[-3:] == ["create", "/git/xfer/base.bundle", "refs/heads/main"]


def test_credentialed_git_runs_only_on_the_platform_repo():
    for op in ("fetch", "push"):
        argv = git_argv(op, repo="acme/web", branch="factory/001-x", expect_sha="")
        assert argv[argv.index("-C") + 1] == GITOPS_REPO and not any("/work" in a for a in argv)


def test_import_bundle_is_tokenless_file_fetch():
    argv = git_argv("import_bundle", repo="acme/web", branch="b")
    assert "protocol.file.allow=always" in argv and "/git/xfer/job.bundle" in argv


def test_hardened_args_reset_helpers_and_disable_hooks():
    a = hardened_git_args()
    pairs = [a[i + 1] for i in range(0, len(a), 2)]
    assert pairs.index("credential.helper=") < pairs.index(
        f"credential.https://github.com.helper={CREDENTIAL_HELPER}"
    )
    assert {
        "core.hooksPath=/dev/null",
        "core.fsmonitor=false",
        "protocol.allow=never",
        "protocol.https.allow=always",
    } <= set(pairs)
    assert HARDENED_GIT_ENV == {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "HOME": "/tmp",
    }


def run_helper(verb: str, stdin: str) -> str:
    script = CREDENTIAL_HELPER[1:] + " " + verb
    r = subprocess.run(
        ["bash", "-c", script],
        input=stdin,
        capture_output=True,
        text=True,
        env={"GIT_TOKEN": "t", "PATH": "/usr/bin:/bin"},
        check=True,
    )
    return r.stdout


def test_credential_helper_answers_github_only():
    assert run_helper("get", "protocol=https\nhost=evil.example\n\n") == ""
    assert "password=t" in run_helper("get", "protocol=https\nhost=github.com\n\n")
    assert run_helper("store", "host=github.com\npassword=t\n\n") == ""


def test_gitops_config_is_hardened():
    cfg = gitops_config(JOB, image=IMG, network="gitops-net", argv=["git"], token="ghs_t")
    assert cfg["Env"].count("GIT_TOKEN=ghs_t") == 1 and "GIT_CONFIG_GLOBAL=/dev/null" in cfg["Env"]
    hc = cfg["HostConfig"]
    assert hc["CapDrop"] == ["ALL"] and not hc.get("Privileged") and not hc.get("Binds")
    assert hc["NetworkMode"] == "gitops-net"
    assert [m["Source"] for m in hc["Mounts"]] == [gitops_volume(JOB)]
    assert hc["ReadonlyRootfs"] and cfg["User"] == "10001"
    assert cfg["Labels"]["factory.kind"] == "gitops"
    assert cfg["Labels"]["factory.managed"] == "true" and cfg["Labels"]["factory.job_id"] == str(
        JOB
    )
    no_tok = gitops_config(JOB, image=IMG, network="gitops-net", argv=["git"], token=None)
    assert not any(e.startswith("GIT_TOKEN") for e in no_tok["Env"])


def test_gitops_network_default():
    assert Settings(mode=FactoryMode.PERSONAL).gitops_network == "gitops-net"
