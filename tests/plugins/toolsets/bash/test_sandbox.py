"""
Tests for bubblewrap-based bash sandboxing.

The argv-construction tests always run. The end-to-end isolation tests are
skipped automatically when a working bwrap jail is not available (e.g. CI
without bubblewrap installed, or kernels with unprivileged user namespaces
disabled).
"""

import os

import pytest

from holmes.plugins.toolsets.bash.common import bash as bash_module
from holmes.plugins.toolsets.bash.common import sandbox as sandbox_module
from holmes.plugins.toolsets.bash.common.bash import execute_bash_command
from holmes.plugins.toolsets.bash.common.sandbox import (
    build_sandbox_argv,
    is_sandbox_available,
)

requires_jail = pytest.mark.skipif(
    not is_sandbox_available(),
    reason="bubblewrap jail not available in this environment",
)


class TestBuildSandboxArgv:
    def test_wraps_command_in_bwrap_and_bash(self):
        argv = build_sandbox_argv("echo hi")
        assert argv[0] == "bwrap"
        # The inner command is handed to bash -c at the end.
        assert argv[-3:] == ["/bin/bash", "-c", "echo hi"]

    def test_clears_environment(self):
        argv = build_sandbox_argv("true")
        assert "--clearenv" in argv

    def test_isolates_namespaces_but_not_network(self):
        argv = build_sandbox_argv("true")
        assert "--unshare-user" in argv
        assert "--unshare-pid" in argv
        # Network is intentionally shared so Holmes can reach K8s/Prometheus/etc.
        assert "--unshare-net" not in argv

    def test_does_not_bind_host_secret_dirs(self):
        argv = build_sandbox_argv("true")
        # Collect the source path of every bind mount.
        bound_sources = [
            argv[i + 1]
            for i, tok in enumerate(argv)
            if tok in ("--bind", "--ro-bind", "--ro-bind-try")
        ]
        # Whole /etc is never bound (only curated files); host homes/secrets never bound.
        assert "/etc" not in bound_sources
        for forbidden in ("/home", "/root", "/var/run/secrets"):
            assert not any(
                src == forbidden or src.startswith(forbidden + "/")
                for src in bound_sources
            ), f"{forbidden} must not be bind-mounted into the jail"

    def test_env_allowlist_passes_through_only_allowlisted(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        monkeypatch.setenv("MY_SECRET_TOKEN", "do-not-leak")
        argv = build_sandbox_argv("true")
        # PATH is on the default allowlist; the secret is not.
        assert "PATH" in argv
        assert "MY_SECRET_TOKEN" not in argv
        assert "do-not-leak" not in argv

    def test_env_passthrough_allowlist_opt_in(self, monkeypatch):
        monkeypatch.setattr(
            sandbox_module, "HOLMES_BASH_SANDBOX_ENV_PASSTHROUGH", "KUBECONFIG"
        )
        monkeypatch.setenv("KUBECONFIG", "/some/path")
        argv = build_sandbox_argv("true")
        assert "KUBECONFIG" in argv


@requires_jail
class TestSandboxIsolationEndToEnd:
    """Runs real commands through the jail via execute_bash_command."""

    @pytest.fixture(autouse=True)
    def force_sandbox(self, monkeypatch):
        # Force the execution path to sandbox regardless of the env-var default.
        monkeypatch.setattr(bash_module, "should_sandbox", lambda: True)

    def test_command_still_runs_and_returns_output(self):
        result = execute_bash_command("echo hello-from-jail", timeout=20)
        assert result.return_code == 0
        assert "hello-from-jail" in result.stdout

    def test_environment_is_scrubbed(self, monkeypatch):
        monkeypatch.setenv("LEAKED_SECRET", "TOPSECRET")
        result = execute_bash_command(
            'env | grep LEAKED_SECRET || echo NO_LEAK', timeout=20
        )
        assert "TOPSECRET" not in result.stdout
        assert "NO_LEAK" in result.stdout

    def test_host_home_not_visible(self):
        result = execute_bash_command('ls /home 2>&1 || echo NO_HOME', timeout=20)
        assert "NO_HOME" in result.stdout

    def test_host_secret_file_not_visible(self, tmp_path):
        secret = tmp_path / "creds"
        secret.write_text("aws_secret=SHOULD-NOT-BE-VISIBLE")
        result = execute_bash_command(
            f'cat {secret} 2>&1 || echo NO_FILE', timeout=20
        )
        assert "SHOULD-NOT-BE-VISIBLE" not in result.stdout
        assert "NO_FILE" in result.stdout

    def test_workdir_is_writable_and_ephemeral(self):
        result = execute_bash_command(
            'echo data > ./scratch && cat ./scratch && pwd', timeout=20
        )
        assert result.return_code == 0
        assert "data" in result.stdout
        assert "/work" in result.stdout


def test_sandbox_disabled_by_default():
    # Without opting in, the default env var keeps sandboxing off.
    assert sandbox_module.HOLMES_BASH_SANDBOX_ENABLED in (False, None)


def test_unavailable_jail_does_not_break_execution(monkeypatch):
    # Even if enabled, an unavailable jail must fall back to direct execution.
    monkeypatch.setattr(sandbox_module, "is_sandbox_available", lambda: False)
    monkeypatch.setattr(sandbox_module, "sandbox_enabled", lambda: True)
    assert sandbox_module.should_sandbox() is False
    result = execute_bash_command("echo fallback-ok", timeout=10)
    assert result.return_code == 0
    assert "fallback-ok" in result.stdout
    # Sanity: confirm we are not accidentally in a jail here.
    assert "PATH" in os.environ
