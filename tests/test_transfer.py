"""Tests for secure file transfer."""

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from remctl.transfer import run_scp, run_scp_pull


class TransferTests(unittest.TestCase):
    def test_scp_reports_progress_and_keeps_password_out_of_arguments(self) -> None:
        child = MagicMock()
        child.expect.side_effect = [0, 2, 4]
        child.match.group.side_effect = ["55", "12.5MB/s"]
        child.exitstatus = 0
        child.signalstatus = None
        progress = MagicMock()

        with patch("remctl.transfer.pexpect.spawn", return_value=child) as spawn:
            exit_code, _ = run_scp(
                Path("image.tar"),
                "10.0.0.11",
                "root",
                "secret",
                "/tmp/image.tar",
                progress,
            )

        self.assertEqual(exit_code, 0)
        command, arguments = spawn.call_args.args
        self.assertEqual(command, "scp")
        self.assertNotIn("secret", arguments)
        self.assertIn("root@10.0.0.11:/tmp/image.tar", arguments)
        child.sendline.assert_called_once_with("secret")
        progress.assert_called_once_with(55, "12.5MB/s")

    def test_missing_scp_binary_returns_clean_error(self) -> None:
        with patch("remctl.transfer.pexpect.spawn", side_effect=OSError("missing")):
            exit_code, message = run_scp(
                Path("file.bin"),
                "10.0.0.11",
                "root",
                "secret",
                "/tmp/file.bin",
                MagicMock(),
            )

        self.assertEqual(exit_code, 255)
        self.assertIn("unable to run scp", message)

    def test_scp_pull_uses_remote_source_and_recursive_option(self) -> None:
        child = MagicMock()
        child.expect.side_effect = [0, 4]
        child.exitstatus = 0
        child.signalstatus = None

        with patch("remctl.transfer.pexpect.spawn", return_value=child) as spawn:
            exit_code, _ = run_scp_pull(
                "10.0.0.11",
                "root",
                "secret",
                "/var/log/app",
                Path("downloads"),
                MagicMock(),
                recursive=True,
            )

        self.assertEqual(exit_code, 0)
        arguments = spawn.call_args.args[1]
        self.assertIn("-r", arguments)
        self.assertIn("root@10.0.0.11:/var/log/app", arguments)
        self.assertEqual(arguments[-1], "downloads")

    def test_scp_push_can_transfer_directory_recursively(self) -> None:
        child = MagicMock()
        child.expect.side_effect = [0, 4]
        child.exitstatus = 0
        child.signalstatus = None

        with patch("remctl.transfer.pexpect.spawn", return_value=child) as spawn:
            exit_code, _ = run_scp(
                Path("artifacts"),
                "10.0.0.11",
                "root",
                "secret",
                "/tmp",
                MagicMock(),
                recursive=True,
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("-r", spawn.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
