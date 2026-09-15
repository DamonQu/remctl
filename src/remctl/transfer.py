"""Secure file transfer through the system scp client."""

import io
from collections.abc import Callable
from pathlib import Path

import pexpect

from remctl.ssh import (
    HOST_KEY_LOCK,
    SSH_OPTIONS,
    repair_changed_host_key,
)

ProgressCallback = Callable[[int, str], None]

_PASSWORD_PROMPT = r"(?i)(?:password|passcode):\s*"
_HOST_KEY_PROMPT = r"Are you sure you want to continue connecting.*\?\s*"
_HOST_KEY_CHANGED = r"REMOTE HOST IDENTIFICATION HAS CHANGED"
_PROGRESS = r"(\d{1,3})%\s+\S+\s+([0-9.]+[KMGTP]?B/s)"
_HOST_KEY_CHANGED_EXIT = 254


def _exit_code(child: pexpect.spawn) -> int:
    child.close()
    if child.exitstatus is not None:
        return child.exitstatus
    if child.signalstatus is not None:
        return 128 + child.signalstatus
    return 255


def _run_scp_once(
    arguments: list[str],
    host: str,
    password: str,
    progress: ProgressCallback,
) -> tuple[int, str]:
    """Run one scp transfer attempt and return its transcript."""
    transcript = io.StringIO()
    child = pexpect.spawn(
        "scp",
        [*SSH_OPTIONS, *arguments],
        encoding="utf-8",
        codec_errors="replace",
    )
    child.logfile_read = transcript
    password_sent = False

    try:
        while True:
            match = child.expect(
                [
                    _PASSWORD_PROMPT,
                    _HOST_KEY_PROMPT,
                    _PROGRESS,
                    _HOST_KEY_CHANGED,
                    pexpect.EOF,
                    pexpect.TIMEOUT,
                ],
                timeout=None if password_sent else 30,
            )
            if match == 0:
                if password_sent:
                    child.close(force=True)
                    return 255, transcript.getvalue()
                child.sendline(password)
                password_sent = True
            elif match == 1:
                with HOST_KEY_LOCK:
                    prompt = f"[{host}] {child.before}{child.after}"
                    child.sendline(input(prompt))
            elif match == 2:
                progress(int(child.match.group(1)), child.match.group(2))
            elif match == 3:
                child.close(force=True)
                return _HOST_KEY_CHANGED_EXIT, transcript.getvalue()
            elif match == 4:
                return _exit_code(child), transcript.getvalue()
            else:
                child.close(force=True)
                return 255, "file transfer timed out"
    except (EOFError, KeyboardInterrupt, OSError, pexpect.exceptions.ExceptionPexpect):
        if not child.closed:
            child.close(force=True)
        raise


def _run_scp(
    arguments: list[str],
    host: str,
    password: str,
    progress: ProgressCallback,
) -> tuple[int, str]:
    """Run scp arguments, recovering from a changed host key once."""
    try:
        exit_code, transcript = _run_scp_once(
            arguments,
            host,
            password,
            progress,
        )
    except KeyboardInterrupt:
        return 130, "file transfer interrupted"
    except (EOFError, OSError, pexpect.exceptions.ExceptionPexpect) as error:
        return 255, f"unable to run scp: {error}"

    if exit_code != _HOST_KEY_CHANGED_EXIT:
        return exit_code, transcript

    recovery_code = repair_changed_host_key(host)
    if recovery_code != 0:
        return recovery_code, "changed host key was not repaired"

    try:
        return _run_scp_once(
            arguments,
            host,
            password,
            progress,
        )
    except KeyboardInterrupt:
        return 130, "file transfer interrupted"
    except (EOFError, OSError, pexpect.exceptions.ExceptionPexpect) as error:
        return 255, f"unable to run scp: {error}"


def run_scp(
    local_path: Path,
    host: str,
    username: str,
    password: str,
    remote_path: str,
    progress: ProgressCallback,
    *,
    recursive: bool = False,
) -> tuple[int, str]:
    """Push a local path to a remote host with scp."""
    options = ["-r"] if recursive else []
    return _run_scp(
        [
            *options,
            str(local_path),
            f"{username}@{host}:{remote_path}",
        ],
        host,
        password,
        progress,
    )


def run_scp_pull(
    host: str,
    username: str,
    password: str,
    remote_path: str,
    local_path: Path,
    progress: ProgressCallback,
    *,
    recursive: bool = False,
) -> tuple[int, str]:
    """Pull a remote path to the local filesystem with scp."""
    options = ["-r"] if recursive else []
    return _run_scp(
        [
            *options,
            f"{username}@{host}:{remote_path}",
            str(local_path),
        ],
        host,
        password,
        progress,
    )
