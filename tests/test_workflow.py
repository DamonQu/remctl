"""Tests for action and workflow configuration."""

import tempfile
import unittest
from pathlib import Path

from remctl.workflow import (
    BUILTIN_ACTIONS,
    WorkflowConfigError,
    WorkflowStep,
    load_actions,
    load_workflow,
    render_action,
)


class WorkflowTests(unittest.TestCase):
    def write_config(self, directory: str, name: str, content: str) -> Path:
        path = Path(directory) / name
        path.write_text(content, encoding="utf-8")
        return path

    def test_missing_action_file_uses_builtins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            actions = load_actions(Path(directory) / "missing.yaml")

        self.assertEqual(set(actions), set(BUILTIN_ACTIONS))

    def test_loads_custom_action_and_renders_shell_quoted_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            action_path = self.write_config(
                directory,
                "actions.yaml",
                """\
version: 1
actions:
  extract:
    description: Extract an archive
    command: "tar -xf {remote_path} -C {destination}"
""",
            )
            workflow_path = self.write_config(
                directory,
                "workflows.yaml",
                """\
version: 1
workflows:
  release:
    cleanup: always
    steps:
      - action: extract
        with:
          destination: "/opt/my app"
""",
            )
            selected = load_workflow(
                "release", load_actions(action_path), workflow_path
            )

        command = render_action(
            selected.steps[0],
            {
                "remote_path": "/tmp/release file.tar",
                "host": "server.example.com",
                "username": "alice",
                "local_name": "release file.tar",
            },
        )
        self.assertEqual(
            command,
            "tar -xf '/tmp/release file.tar' -C '/opt/my app'",
        )

    def test_builtin_actions_render_expected_commands(self) -> None:
        context = {
            "remote_path": "/tmp/upload file",
            "host": "server.example.com",
            "username": "alice",
            "local_name": "upload file",
        }
        cases = [
            (
                "copy",
                {"destination": "/opt/my app/app.jar"},
                "cp -- '/tmp/upload file' '/opt/my app/app.jar'",
            ),
            (
                "restart-service",
                {"service": "hcd admin"},
                "systemctl restart 'hcd admin'",
            ),
            (
                "docker-image-load",
                {},
                "docker image load --input '/tmp/upload file'",
            ),
            (
                "restart-container",
                {"container": "orion app"},
                "docker restart 'orion app'",
            ),
        ]
        for name, values, expected in cases:
            with self.subTest(action=name):
                step = WorkflowStep(BUILTIN_ACTIONS[name], values)
                self.assertEqual(render_action(step, context), expected)

    def test_custom_action_cannot_override_builtin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                "actions.yaml",
                """\
version: 1
actions:
  copy:
    command: "echo replaced"
""",
            )
            with self.assertRaisesRegex(WorkflowConfigError, "cannot override"):
                load_actions(path)

    def test_workflow_requires_cleanup_policy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                "workflows.yaml",
                """\
version: 1
workflows:
  release:
    steps:
      - action: docker-image-load
""",
            )
            with self.assertRaisesRegex(WorkflowConfigError, "cleanup must be"):
                load_workflow("release", dict(BUILTIN_ACTIONS), path)

    def test_workflow_rejects_unknown_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                "workflows.yaml",
                """\
version: 1
workflows:
  release:
    cleanup: never
    steps:
      - action: missing
""",
            )
            with self.assertRaisesRegex(WorkflowConfigError, "unknown action"):
                load_workflow("release", dict(BUILTIN_ACTIONS), path)

    def test_workflow_rejects_missing_and_unused_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing_path = self.write_config(
                directory,
                "missing.yaml",
                """\
version: 1
workflows:
  release:
    cleanup: success
    steps:
      - action: copy
""",
            )
            extra_path = self.write_config(
                directory,
                "extra.yaml",
                """\
version: 1
workflows:
  release:
    cleanup: success
    steps:
      - action: docker-image-load
        with:
          typo: value
""",
            )
            with self.assertRaisesRegex(WorkflowConfigError, "missing parameters"):
                load_workflow("release", dict(BUILTIN_ACTIONS), missing_path)
            with self.assertRaisesRegex(WorkflowConfigError, "unused parameters"):
                load_workflow("release", dict(BUILTIN_ACTIONS), extra_path)

    def test_workflow_cannot_override_runtime_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_config(
                directory,
                "workflows.yaml",
                """\
version: 1
workflows:
  release:
    cleanup: always
    steps:
      - action: docker-image-load
        with:
          remote_path: /tmp/other
""",
            )
            with self.assertRaisesRegex(WorkflowConfigError, "cannot override"):
                load_workflow("release", dict(BUILTIN_ACTIONS), path)

    def test_invalid_yaml_version_and_placeholder_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            version_path = self.write_config(
                directory,
                "version.yaml",
                "version: 2\nactions: {}\n",
            )
            placeholder_path = self.write_config(
                directory,
                "placeholder.yaml",
                """\
version: 1
actions:
  invalid:
    command: "echo {value.attr}"
""",
            )
            with self.assertRaisesRegex(WorkflowConfigError, "version: 1"):
                load_actions(version_path)
            with self.assertRaisesRegex(
                WorkflowConfigError, "invalid action placeholder"
            ):
                load_actions(placeholder_path)

    def test_unknown_root_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            action_path = self.write_config(
                directory,
                "actions.yaml",
                "version: 1\naction: {}\nactions: {}\n",
            )
            workflow_path = self.write_config(
                directory,
                "workflows.yaml",
                "version: 1\nworkflow: {}\nworkflows: {}\n",
            )
            with self.assertRaisesRegex(WorkflowConfigError, "unknown fields"):
                load_actions(action_path)
            with self.assertRaisesRegex(WorkflowConfigError, "unknown fields"):
                load_workflow("release", dict(BUILTIN_ACTIONS), workflow_path)


if __name__ == "__main__":
    unittest.main()
