import logging
import subprocess
from dataclasses import dataclass
from typing import Optional

from holmes.plugins.toolsets.bash.common.sandbox import (
    build_sandbox_argv,
    should_sandbox,
)
from holmes.utils.memory_limit import check_oom_and_append_hint, get_ulimit_prefix

logger = logging.getLogger(__name__)


@dataclass
class BashResult:
    """Simple result type for bash command execution."""

    stdout: str
    return_code: Optional[int]
    timed_out: bool


def execute_bash_command(cmd: str, timeout: int) -> BashResult:
    """
    Execute a bash command and return the result.

    Args:
        cmd: The bash command to execute
        timeout: Timeout in seconds

    Returns:
        BashResult with stdout, return_code, and timed_out flag
    """
    protected_cmd = get_ulimit_prefix() + cmd

    if should_sandbox():
        # Run inside a bubblewrap jail. The ulimit-prefixed command is handed to
        # bash -c *inside* the jail, so shell semantics are preserved while the
        # host environment, credentials, and filesystem are isolated away.
        logger.debug("Executing bash command inside bubblewrap sandbox")
        popen_args: list = build_sandbox_argv(protected_cmd)
        popen_kwargs: dict = {}
    else:
        popen_args = protected_cmd  # type: ignore[assignment]
        popen_kwargs = {"shell": True, "executable": "/bin/bash"}

    process = subprocess.Popen(
        popen_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        **popen_kwargs,
    )

    try:
        stdout, _ = process.communicate(timeout=timeout)
        stdout = stdout.strip() if stdout else ""
        stdout = check_oom_and_append_hint(stdout, process.returncode)

        return BashResult(
            stdout=stdout,
            return_code=process.returncode,
            timed_out=False,
        )
    except subprocess.TimeoutExpired:
        process.kill()
        # Collect any partial output that was generated before timeout
        stdout, _ = process.communicate()
        stdout = stdout.strip() if stdout else ""

        return BashResult(
            stdout=stdout,
            return_code=None,
            timed_out=True,
        )
