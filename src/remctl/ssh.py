"""OpenSSH integration for credential validation and interactive sessions."""

import shutil
import signal
import subprocess
import sys
from collections.abc import Sequence

import pexpect

SSH_OPTIONS = (
    "-o",
    "PreferredAuthentications=keyboard-interactive,password",
    "-o",
    "PubkeyAuthentication=no",
    "-o",
    "NumberOfPasswordPrompts=1",
    "-o",
    "StrictHostKeyChecking=ask",
    "-o",
    "ConnectTimeout=10",
)

_PASSWORD_PROMPT = r"(?i)(?:password|passcode):\s*"
_HOST_KEY_PROMPT = r"Are you sure you want to continue connecting.*\?\s*"
_AUTHENTICATED = r"Authenticated to .+ using .+\."
_HOST_KEY_CHANGED = r"REMOTE HOST IDENTIFICATION HAS CHANGED"
_HOST_KEY_CHANGED_EXIT = 254


def _exit_code(child: pexpect.spawn) -> int:
    """Close a child process and return a shell-style exit code."""
    child.close()
    if child.exitstatus is not None:
        return child.exitstatus
    if child.signalstatus is not None:
        return 128 + child.signalstatus
    return 255


def _resize_child(child: pexpect.spawn) -> None:
    """Update the child pseudo-terminal to match the local terminal."""
    columns, rows = shutil.get_terminal_size(fallback=(80, 24))
    try:
        child.setwinsize(rows, columns)
    except (OSError, pexpect.exceptions.ExceptionPexpect):
        pass


def _run_ssh_once(
    host: str,
    username: str,
    password: str,
    *,
    remote_command: Sequence[str] = (),
    interactive: bool = True,
    authentication_only: bool = False,
) -> int:
    """Run one OpenSSH attempt, supplying one password."""
    destination = f"{username}@{host}"
    columns, rows = shutil.get_terminal_size(fallback=(80, 24))
    if authentication_only:
        mode_options = ("-N", "-v")
    elif not interactive:
        mode_options = ("-T",)
    else:
        mode_options = ()
    child = pexpect.spawn(
        "ssh",
        [*SSH_OPTIONS, *mode_options, destination, *remote_command],
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(rows, columns),
    )
    child.logfile_read = sys.stdout
    password_sent = False
    previous_resize_handler = None
    if interactive and hasattr(signal, "SIGWINCH"):
        try:
            previous_resize_handler = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, lambda signum, frame: _resize_child(child))
        except (OSError, ValueError):
            previous_resize_handler = None

    try:
        while True:
            match = child.expect(
                [
                    _PASSWORD_PROMPT,
                    _HOST_KEY_PROMPT,
                    _AUTHENTICATED,
                    _HOST_KEY_CHANGED,
                    pexpect.EOF,
                    pexpect.TIMEOUT,
                ],
                timeout=30,
            )
            if match == 0:
                if password_sent:
                    child.close(force=True)
                    return 255
                child.sendline(password)
                password_sent = True
                if interactive:
                    child.logfile_read = None
                    child.interact()
                    return _exit_code(child)
                if not authentication_only:
                    child.expect(pexpect.EOF, timeout=None)
                    return _exit_code(child)
            elif match == 1:
                child.sendline(input())
            elif match == 2:
                if authentication_only:
                    child.close(force=True)
                    return 0
            elif match == 3:
                child.close(force=True)
                return _HOST_KEY_CHANGED_EXIT
            elif match == 4:
                return _exit_code(child)
            else:
                print("error: SSH connection timed out", file=sys.stderr)
                child.close(force=True)
                return 255
    except (EOFError, KeyboardInterrupt, OSError, pexpect.exceptions.ExceptionPexpect):
        if not child.closed:
            child.close(force=True)
        raise
    finally:
        if previous_resize_handler is not None:
            signal.signal(signal.SIGWINCH, previous_resize_handler)


def run_ssh(
    host: str,
    username: str,
    password: str,
    *,
    remote_command: Sequence[str] = (),
    interactive: bool = True,
    authentication_only: bool = False,
) -> int:
    """Run OpenSSH and optionally recover from a changed known-host key."""
    try:
        exit_code = _run_ssh_once(
            host,
            username,
            password,
            remote_command=remote_command,
            interactive=interactive,
            authentication_only=authentication_only,
        )
    except KeyboardInterrupt:
        print("\nSSH session interrupted.", file=sys.stderr)
        return 130
    except (EOFError, OSError, pexpect.exceptions.ExceptionPexpect) as error:
        print(f"error: unable to run SSH: {error}", file=sys.stderr)
        return 255
    if exit_code != _HOST_KEY_CHANGED_EXIT:
        return exit_code

    try:
        answer = input(
            f"\nHost key for {host} changed. Remove the old key and retry? [y/N]: "
        )
    except KeyboardInterrupt:
        print("\nSSH session interrupted.", file=sys.stderr)
        return 130
    except EOFError:
        print("error: unable to confirm host key removal", file=sys.stderr)
        return 255
    if answer.strip().lower() not in {"y", "yes"}:
        print("error: old host key was not removed", file=sys.stderr)
        return 255

    try:
        result = subprocess.run(["ssh-keygen", "-R", host], check=False)
    except KeyboardInterrupt:
        print("\nSSH session interrupted.", file=sys.stderr)
        return 130
    except OSError as error:
        print(f"error: unable to run ssh-keygen: {error}", file=sys.stderr)
        return 255
    if result.returncode != 0:
        print(f"error: unable to remove the old host key for {host}", file=sys.stderr)
        return 255

    print(f"Old host key removed for {host}; retrying SSH connection...")
    try:
        return _run_ssh_once(
            host,
            username,
            password,
            remote_command=remote_command,
            interactive=interactive,
            authentication_only=authentication_only,
        )
    except KeyboardInterrupt:
        print("\nSSH session interrupted.", file=sys.stderr)
        return 130
    except (EOFError, OSError, pexpect.exceptions.ExceptionPexpect) as error:
        print(f"error: unable to run SSH: {error}", file=sys.stderr)
        return 255


def validate_ssh_credential(host: str, username: str, password: str) -> bool:
    """Verify SSH authentication without running a remote command."""
    return (
        run_ssh(
            host,
            username,
            password,
            interactive=False,
            authentication_only=True,
        )
        == 0
    )
