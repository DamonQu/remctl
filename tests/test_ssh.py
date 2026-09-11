"""Tests for OpenSSH integration."""

import contextlib
import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from remctl.ssh import _run_ssh_once, run_ssh, validate_ssh_credential


class SshTests(unittest.TestCase):
    def test_validation_checks_authentication_without_remote_command(self) -> None:
        with patch("remctl.ssh.run_ssh", return_value=0) as run_ssh:
            valid = validate_ssh_credential("10.0.0.11", "root", "secret")

        self.assertTrue(valid)
        run_ssh.assert_called_once_with(
            "10.0.0.11",
            "root",
            "secret",
            interactive=False,
            authentication_only=True,
        )

    def test_validation_rejects_failed_login(self) -> None:
        with patch("remctl.ssh.run_ssh", return_value=255):
            valid = validate_ssh_credential("10.0.0.11", "root", "wrong")

        self.assertFalse(valid)

    def test_authentication_only_succeeds_on_authenticated_event(self) -> None:
        child = MagicMock()
        child.expect.side_effect = [0, 2]

        with patch("remctl.ssh.pexpect.spawn", return_value=child) as spawn:
            exit_code = _run_ssh_once(
                "10.0.0.11",
                "root",
                "secret",
                interactive=False,
                authentication_only=True,
            )

        self.assertEqual(exit_code, 0)
        arguments = spawn.call_args.args[1]
        self.assertIn("-N", arguments)
        self.assertIn("-v", arguments)
        child.sendline.assert_called_once_with("secret")
        child.close.assert_called_once_with(force=True)

    def test_changed_host_key_can_be_removed_and_retried(self) -> None:
        output = io.StringIO()
        with (
            patch("remctl.ssh._run_ssh_once", side_effect=[254, 0]) as run_once,
            patch("builtins.input", return_value="yes"),
            patch(
                "remctl.ssh.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as subprocess_run,
            contextlib.redirect_stdout(output),
        ):
            exit_code = run_ssh("10.0.0.11", "root", "secret")

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_once.call_count, 2)
        subprocess_run.assert_called_once_with(
            ["ssh-keygen", "-R", "10.0.0.11"], check=False
        )

    def test_changed_host_key_is_kept_without_confirmation(self) -> None:
        error = io.StringIO()
        with (
            patch("remctl.ssh._run_ssh_once", return_value=254),
            patch("builtins.input", return_value="no"),
            patch("remctl.ssh.subprocess.run") as subprocess_run,
            contextlib.redirect_stderr(error),
        ):
            exit_code = run_ssh("10.0.0.11", "root", "secret")

        self.assertEqual(exit_code, 255)
        subprocess_run.assert_not_called()

    def test_password_is_sent_to_tty_not_process_arguments(self) -> None:
        child = MagicMock()
        child.expect.side_effect = [0, 4]
        child.exitstatus = 0
        child.signalstatus = None

        with patch("remctl.ssh.pexpect.spawn", return_value=child) as spawn:
            exit_code = _run_ssh_once(
                "10.0.0.11",
                "root",
                "secret",
                remote_command=("true",),
                interactive=False,
            )

        self.assertEqual(exit_code, 0)
        command, arguments = spawn.call_args.args
        self.assertEqual(command, "ssh")
        self.assertNotIn("secret", arguments)
        self.assertIn("root@10.0.0.11", arguments)
        child.sendline.assert_called_once_with("secret")

    def test_missing_ssh_binary_returns_clean_error(self) -> None:
        error = io.StringIO()

        with (
            patch("remctl.ssh.pexpect.spawn", side_effect=OSError("not found")),
            contextlib.redirect_stderr(error),
        ):
            exit_code = run_ssh("10.0.0.11", "root", "secret")

        self.assertEqual(exit_code, 255)
        self.assertIn("unable to run SSH", error.getvalue())

    def test_interactive_session_installs_and_restores_resize_handler(self) -> None:
        child = MagicMock()
        child.expect.return_value = 0
        child.exitstatus = 0
        child.signalstatus = None
        previous_handler = object()

        with (
            patch("remctl.ssh.pexpect.spawn", return_value=child),
            patch(
                "remctl.ssh.shutil.get_terminal_size",
                return_value=os.terminal_size((120, 40)),
            ),
            patch("remctl.ssh.signal.getsignal", return_value=previous_handler),
            patch("remctl.ssh.signal.signal") as set_signal,
        ):
            exit_code = _run_ssh_once("10.0.0.11", "root", "secret")
            resize_handler = set_signal.call_args_list[0].args[1]
            resize_handler(None, None)

        self.assertEqual(exit_code, 0)
        child.interact.assert_called_once_with()
        self.assertEqual(set_signal.call_count, 2)
        self.assertIs(set_signal.call_args_list[-1].args[1], previous_handler)
        child.setwinsize.assert_called_once_with(40, 120)

    def test_interrupted_session_closes_child_and_returns_130(self) -> None:
        error = io.StringIO()
        child = MagicMock()
        child.expect.side_effect = KeyboardInterrupt
        child.closed = False

        with (
            patch("remctl.ssh.pexpect.spawn", return_value=child),
            contextlib.redirect_stderr(error),
        ):
            exit_code = run_ssh("10.0.0.11", "root", "secret")

        self.assertEqual(exit_code, 130)
        child.close.assert_called_once_with(force=True)
        self.assertIn("interrupted", error.getvalue())


if __name__ == "__main__":
    unittest.main()
