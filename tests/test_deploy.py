"""Tests for deployment workflows."""

import io
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from remctl.deploy import (
    DeployResult,
    DeployTarget,
    ProgressDisplay,
    deploy_many,
    deploy_to_target,
)
from remctl.workflow import (
    BUILTIN_ACTIONS,
    CleanupPolicy,
    WorkflowDefinition,
    WorkflowStep,
)


def workflow(
    cleanup: CleanupPolicy,
    *steps: tuple[str, dict[str, str]],
) -> WorkflowDefinition:
    return WorkflowDefinition(
        "test-workflow",
        cleanup,
        tuple(WorkflowStep(BUILTIN_ACTIONS[name], values) for name, values in steps),
    )


class DeployTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = DeployTarget("10.0.0.11", "root", "secret")
        self.stream = io.StringIO()
        self.display = ProgressDisplay([self.target.host], self.stream)

    def test_upload_without_workflow_retains_temporary_path(self) -> None:
        with (
            patch("remctl.deploy._temporary_path", return_value="/tmp/upload.bin"),
            patch("remctl.deploy.run_scp", return_value=(0, "")),
            patch("remctl.deploy.run_ssh") as run_ssh,
        ):
            result = deploy_to_target(
                Path("upload.bin"), self.target, None, self.display
            )

        self.assertTrue(result.success)
        self.assertEqual(result.summary, "uploaded to /tmp/upload.bin")
        run_ssh.assert_not_called()

    def test_progress_display_includes_percentage_and_bandwidth(self) -> None:
        self.display.update("10.0.0.11", "transferring", 42, "8.5MB/s")
        rendered = self.stream.getvalue()
        self.assertIn("[10.0.0.11]", rendered)
        self.assertIn("42%", rendered)
        self.assertIn("8.5MB/s", rendered)

    def test_workflow_runs_steps_in_order_and_cleans_up(self) -> None:
        commands: list[tuple[str, ...]] = []

        def run_ssh(host, username, password, *, remote_command, **kwargs):
            commands.append(remote_command)
            return 0

        selected = workflow(
            CleanupPolicy.ALWAYS,
            ("copy", {"destination": "/opt/app/app.jar"}),
            ("restart-service", {"service": "hcdadmin"}),
        )
        with (
            patch("remctl.deploy._temporary_path", return_value="/tmp/app.jar"),
            patch("remctl.deploy.run_scp", return_value=(0, "")),
            patch("remctl.deploy.run_ssh", side_effect=run_ssh),
        ):
            result = deploy_to_target(
                Path("app.jar"), self.target, selected, self.display
            )

        self.assertTrue(result.success)
        self.assertEqual(
            commands,
            [
                ("cp -- /tmp/app.jar /opt/app/app.jar",),
                ("systemctl restart hcdadmin",),
                ("rm -f -- /tmp/app.jar",),
            ],
        )

    def test_failed_step_stops_and_always_cleans_up(self) -> None:
        commands: list[tuple[str, ...]] = []

        def run_ssh(host, username, password, *, remote_command, **kwargs):
            commands.append(remote_command)
            return 1 if remote_command[0].startswith("cp ") else 0

        selected = workflow(
            CleanupPolicy.ALWAYS,
            ("copy", {"destination": "/opt/app/app.jar"}),
            ("restart-service", {"service": "hcdadmin"}),
        )
        with (
            patch("remctl.deploy._temporary_path", return_value="/tmp/app.jar"),
            patch("remctl.deploy.run_scp", return_value=(0, "")),
            patch("remctl.deploy.run_ssh", side_effect=run_ssh),
        ):
            result = deploy_to_target(
                Path("app.jar"), self.target, selected, self.display
            )

        self.assertFalse(result.success)
        self.assertEqual(
            commands,
            [("cp -- /tmp/app.jar /opt/app/app.jar",), ("rm -f -- /tmp/app.jar",)],
        )
        self.assertIn("failed at copy", result.summary)

    def test_success_cleanup_policy_retains_file_after_step_failure(self) -> None:
        selected = workflow(CleanupPolicy.SUCCESS, ("docker-image-load", {}))
        with (
            patch("remctl.deploy._temporary_path", return_value="/tmp/image.tar"),
            patch("remctl.deploy.run_scp", return_value=(0, "")),
            patch("remctl.deploy.run_ssh", return_value=1) as run_ssh,
        ):
            result = deploy_to_target(
                Path("image.tar"), self.target, selected, self.display
            )

        self.assertFalse(result.success)
        run_ssh.assert_called_once()

    def test_success_cleanup_policy_cleans_after_success(self) -> None:
        selected = workflow(CleanupPolicy.SUCCESS, ("docker-image-load", {}))
        with (
            patch("remctl.deploy._temporary_path", return_value="/tmp/image.tar"),
            patch("remctl.deploy.run_scp", return_value=(0, "")),
            patch("remctl.deploy.run_ssh", side_effect=[0, 0]) as run_ssh,
        ):
            result = deploy_to_target(
                Path("image.tar"), self.target, selected, self.display
            )

        self.assertTrue(result.success)
        self.assertEqual(run_ssh.call_count, 2)
        self.assertEqual(
            run_ssh.call_args.kwargs["remote_command"],
            ("rm -f -- /tmp/image.tar",),
        )

    def test_never_cleanup_retains_file_after_success(self) -> None:
        selected = workflow(
            CleanupPolicy.NEVER,
            ("restart-container", {"container": "orion"}),
        )
        with (
            patch("remctl.deploy._temporary_path", return_value="/tmp/file.bin"),
            patch("remctl.deploy.run_scp", return_value=(0, "")),
            patch("remctl.deploy.run_ssh", return_value=0) as run_ssh,
        ):
            result = deploy_to_target(
                Path("file.bin"), self.target, selected, self.display
            )

        self.assertTrue(result.success)
        self.assertIn("retained at /tmp/file.bin", result.summary)
        run_ssh.assert_called_once()

    def test_transfer_failure_obeys_always_cleanup(self) -> None:
        selected = workflow(CleanupPolicy.ALWAYS, ("docker-image-load", {}))
        with (
            patch("remctl.deploy._temporary_path", return_value="/tmp/image.tar"),
            patch("remctl.deploy.run_scp", return_value=(1, "transfer error\n")),
            patch("remctl.deploy.run_ssh", return_value=0) as run_ssh,
        ):
            result = deploy_to_target(
                Path("image.tar"), self.target, selected, self.display
            )

        self.assertFalse(result.success)
        self.assertEqual(
            run_ssh.call_args.kwargs["remote_command"],
            ("rm -f -- /tmp/image.tar",),
        )

    def test_cleanup_failure_is_reported(self) -> None:
        selected = workflow(CleanupPolicy.ALWAYS, ("docker-image-load", {}))
        with (
            patch("remctl.deploy._temporary_path", return_value="/tmp/image.tar"),
            patch("remctl.deploy.run_scp", return_value=(0, "")),
            patch("remctl.deploy.run_ssh", side_effect=[0, 1]),
        ):
            result = deploy_to_target(
                Path("image.tar"), self.target, selected, self.display
            )

        self.assertFalse(result.success)
        self.assertIn("cleanup failed", result.summary)

    def test_multiple_targets_run_concurrently_and_keep_result_order(self) -> None:
        targets = [
            DeployTarget("10.0.0.11", "root", "one"),
            DeployTarget("10.0.0.12", "root", "two"),
        ]
        barrier = threading.Barrier(2, timeout=2)

        def deploy(local_path, target, selected_workflow, display):
            barrier.wait()
            return DeployResult(target.host, True, "done")

        with patch("remctl.deploy.deploy_to_target", side_effect=deploy):
            results = deploy_many(Path("image.tar"), targets, None, io.StringIO())

        self.assertEqual(
            [result.host for result in results],
            ["10.0.0.11", "10.0.0.12"],
        )


if __name__ == "__main__":
    unittest.main()
