"""Tests for deployment workflows."""

import io
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from remctl.deploy import (
    ARCHON_PATH,
    HCDMGMT_PATH,
    DeployOperation,
    DeployResult,
    DeployTarget,
    ProgressDisplay,
    deploy_many,
    deploy_to_target,
)


class DeployTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = DeployTarget("10.0.0.11", "root", "secret")
        self.stream = io.StringIO()
        self.display = ProgressDisplay([self.target.host], self.stream)

    def test_file_only_returns_remote_temporary_path(self) -> None:
        with (
            patch("remctl.deploy._temporary_path", return_value="/tmp/upload.bin"),
            patch("remctl.deploy.run_scp", return_value=(0, "")),
        ):
            result = deploy_to_target(
                Path("upload.bin"),
                self.target,
                DeployOperation.FILE,
                self.display,
            )

        self.assertTrue(result.success)
        self.assertEqual(result.summary, "uploaded to /tmp/upload.bin")

    def test_progress_display_includes_percentage_and_bandwidth(self) -> None:
        self.display.update("10.0.0.11", "transferring", 42, "8.5MB/s")

        rendered = self.stream.getvalue()
        self.assertIn("[10.0.0.11]", rendered)
        self.assertIn("42%", rendered)
        self.assertIn("8.5MB/s", rendered)

    def test_failed_transfer_cleans_up_partial_deployment_file(self) -> None:
        commands: list[tuple[str, ...]] = []

        def run_ssh(host, username, password, *, remote_command, **kwargs):
            commands.append(remote_command)
            return 0

        with (
            patch("remctl.deploy._temporary_path", return_value="/tmp/image.tar"),
            patch("remctl.deploy.run_scp", return_value=(1, "transfer error\n")),
            patch("remctl.deploy.run_ssh", side_effect=run_ssh),
        ):
            result = deploy_to_target(
                Path("image.tar"),
                self.target,
                DeployOperation.IMAGE,
                self.display,
            )

        self.assertFalse(result.success)
        self.assertEqual(commands, [("rm", "-f", "--", "/tmp/image.tar")])

    def test_failed_file_only_transfer_does_not_delete_remote_destination(self) -> None:
        with (
            patch("remctl.deploy._temporary_path", return_value="/tmp/upload.bin"),
            patch("remctl.deploy.run_scp", return_value=(1, "transfer error\n")),
            patch("remctl.deploy.run_ssh") as run_ssh,
        ):
            result = deploy_to_target(
                Path("upload.bin"),
                self.target,
                DeployOperation.FILE,
                self.display,
            )

        self.assertFalse(result.success)
        run_ssh.assert_not_called()

    def test_docker_image_load_reports_image_reference_and_cleans_up(self) -> None:
        commands: list[tuple[str, ...]] = []

        def run_ssh(host, username, password, *, remote_command, **kwargs):
            commands.append(remote_command)
            if remote_command[:3] == ("docker", "image", "load"):
                kwargs["output"].write("Loaded image: example/app:1.0\n")
            return 0

        with (
            patch("remctl.deploy._temporary_path", return_value="/tmp/image.tar"),
            patch("remctl.deploy.run_scp", return_value=(0, "")),
            patch("remctl.deploy.run_ssh", side_effect=run_ssh),
        ):
            result = deploy_to_target(
                Path("image.tar"),
                self.target,
                DeployOperation.IMAGE,
                self.display,
            )

        self.assertTrue(result.success)
        self.assertIn("example/app:1.0", result.summary)
        self.assertEqual(
            commands,
            [
                ("docker", "image", "load", "--input", "/tmp/image.tar"),
                ("rm", "-f", "--", "/tmp/image.tar"),
            ],
        )

    def test_archon_deploy_copies_cleans_and_restarts(self) -> None:
        commands: list[tuple[str, ...]] = []

        def run_ssh(host, username, password, *, remote_command, **kwargs):
            commands.append(remote_command)
            return 0

        with (
            patch("remctl.deploy._temporary_path", return_value="/tmp/archon.jar"),
            patch("remctl.deploy.run_scp", return_value=(0, "")),
            patch("remctl.deploy.run_ssh", side_effect=run_ssh),
        ):
            result = deploy_to_target(
                Path("archon.jar"),
                self.target,
                DeployOperation.ARCHON,
                self.display,
            )

        self.assertTrue(result.success)
        self.assertEqual(
            commands,
            [
                ("cp", "--", "/tmp/archon.jar", ARCHON_PATH),
                ("rm", "-f", "--", "/tmp/archon.jar"),
                ("systemctl", "restart", "hcdadmin"),
            ],
        )

    def test_hcdmgmt_deploy_uses_expected_path_and_service(self) -> None:
        commands: list[tuple[str, ...]] = []

        def run_ssh(host, username, password, *, remote_command, **kwargs):
            commands.append(remote_command)
            return 0

        with (
            patch("remctl.deploy._temporary_path", return_value="/tmp/hcdmgmt.jar"),
            patch("remctl.deploy.run_scp", return_value=(0, "")),
            patch("remctl.deploy.run_ssh", side_effect=run_ssh),
        ):
            result = deploy_to_target(
                Path("hcdmgmt.jar"),
                self.target,
                DeployOperation.HCDMGMT,
                self.display,
            )

        self.assertTrue(result.success)
        self.assertEqual(
            commands,
            [
                ("cp", "--", "/tmp/hcdmgmt.jar", HCDMGMT_PATH),
                ("rm", "-f", "--", "/tmp/hcdmgmt.jar"),
                ("systemctl", "restart", "hcdmgmt"),
            ],
        )

    def test_multiple_targets_run_concurrently_and_keep_result_order(self) -> None:
        targets = [
            DeployTarget("10.0.0.11", "root", "one"),
            DeployTarget("10.0.0.12", "root", "two"),
        ]
        barrier = threading.Barrier(2, timeout=2)

        def deploy(local_path, target, operation, display):
            barrier.wait()
            return DeployResult(target.host, True, "done")

        with patch("remctl.deploy.deploy_to_target", side_effect=deploy):
            results = deploy_many(
                Path("image.tar"),
                targets,
                DeployOperation.IMAGE,
                io.StringIO(),
            )

        self.assertEqual(
            [result.host for result in results],
            ["10.0.0.11", "10.0.0.12"],
        )


if __name__ == "__main__":
    unittest.main()
