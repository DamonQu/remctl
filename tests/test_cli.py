"""Tests for the remctl command-line interface."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import keyring

from remctl.cli import HostCredential, main
from remctl.deploy import DeployResult
from remctl.workflow import WorkflowConfigError


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.initialize_config_patcher = patch("remctl.cli.initialize_config")
        self.initialize_config = self.initialize_config_patcher.start()
        self.addCleanup(self.initialize_config_patcher.stop)

    def test_no_arguments_prints_help(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("usage: rctl", output.getvalue())
        self.initialize_config.assert_not_called()

    def test_normal_command_initializes_configuration(self) -> None:
        with (
            patch("remctl.cli.load_host_index", return_value={}),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            exit_code = main(["list"])

        self.assertEqual(exit_code, 0)
        self.initialize_config.assert_called_once_with()

    def test_configuration_initialization_failure_stops_command(self) -> None:
        error = io.StringIO()
        self.initialize_config.side_effect = PermissionError("denied")
        with (
            patch("remctl.cli.load_host_index") as load_index,
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(["list"])

        self.assertEqual(exit_code, 1)
        load_index.assert_not_called()
        self.assertIn("unable to initialize", error.getvalue())

    def test_add_host_saves_credential_in_keyring(self) -> None:
        output = io.StringIO()

        with (
            patch("builtins.input", return_value="alice"),
            patch("remctl.cli.getpass.getpass", return_value="secret"),
            patch(
                "remctl.cli.validate_ssh_credential", return_value=True
            ) as validate,
            patch("remctl.cli.keyring.get_password", return_value=None),
            patch("remctl.cli.keyring.set_password") as set_password,
            patch("remctl.cli.register_host") as register_host,
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["add", "example.com"])

        self.assertEqual(exit_code, 0)
        validate.assert_called_once_with(
            "example.com", "alice", "secret", debug=False
        )
        set_password.assert_called_once_with("remctl:example.com", "alice", "secret")
        register_host.assert_called_once_with("example.com", "alice")
        self.assertIn("Credential saved for alice@example.com", output.getvalue())
        self.assertNotIn("Validating SSH", output.getvalue())

    def test_add_host_debug_shows_validation_diagnostics(self) -> None:
        output = io.StringIO()

        with (
            patch("builtins.input", return_value="alice"),
            patch("remctl.cli.getpass.getpass", return_value="secret"),
            patch(
                "remctl.cli.validate_ssh_credential", return_value=True
            ) as validate,
            patch("remctl.cli.keyring.get_password", return_value=None),
            patch("remctl.cli.keyring.set_password"),
            patch("remctl.cli.register_host"),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["add", "example.com", "--debug"])

        self.assertEqual(exit_code, 0)
        validate.assert_called_once_with(
            "example.com", "alice", "secret", debug=True
        )
        self.assertIn(
            "Validating SSH credential for alice@example.com",
            output.getvalue(),
        )

    def test_add_host_does_not_save_invalid_ssh_credential(self) -> None:
        error = io.StringIO()
        output = io.StringIO()

        with (
            patch("builtins.input", return_value="alice"),
            patch("remctl.cli.getpass.getpass", return_value="wrong"),
            patch("remctl.cli.validate_ssh_credential", return_value=False),
            patch("remctl.cli.keyring.set_password") as set_password,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(["add", "example.com"])

        self.assertEqual(exit_code, 1)
        set_password.assert_not_called()
        self.assertIn("validation failed", error.getvalue())
        self.assertNotIn("Validating SSH", output.getvalue())

    def test_add_host_rejects_empty_username(self) -> None:
        error = io.StringIO()

        with (
            patch("builtins.input", return_value="  "),
            patch("remctl.cli.keyring.set_password") as set_password,
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(["add", "example.com"])

        self.assertEqual(exit_code, 2)
        set_password.assert_not_called()
        self.assertIn("username cannot be empty", error.getvalue())

    def test_get_host_shows_username_without_password(self) -> None:
        output = io.StringIO()
        with (
            patch(
                "remctl.cli.load_host_index",
                return_value={"example.com": ["alice"]},
            ),
            patch("remctl.cli.keyring.get_password", return_value="secret"),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["get", "example.com"])

        self.assertEqual(exit_code, 0)
        self.assertIn("username: alice", output.getvalue())
        self.assertIn("password: stored", output.getvalue())
        self.assertNotIn("secret", output.getvalue())

    def test_get_host_reports_missing_credential(self) -> None:
        error = io.StringIO()

        with (
            patch("remctl.cli.load_host_index", return_value={}),
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(["get", "missing.example.com"])

        self.assertEqual(exit_code, 1)
        self.assertIn("no credential found", error.getvalue())

    def test_delete_host_removes_credential(self) -> None:
        output = io.StringIO()
        with (
            patch(
                "remctl.cli.load_host_index",
                return_value={"example.com": ["alice"]},
            ),
            patch("remctl.cli.keyring.get_password", return_value="secret"),
            patch("remctl.cli.keyring.delete_password") as delete_password,
            patch("remctl.cli.unregister_host") as unregister_host,
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["delete", "example.com"])

        self.assertEqual(exit_code, 0)
        delete_password.assert_called_once_with("remctl:example.com", "alice")
        unregister_host.assert_called_once_with("example.com", "alice")
        self.assertIn("Credential deleted for alice@example.com", output.getvalue())

    def test_list_hosts_shows_hosts_and_usernames_without_passwords(self) -> None:
        output = io.StringIO()
        passwords = {"dbadmin": "db-secret", "deploy": "web-secret"}

        with (
            patch(
                "remctl.cli.load_host_index",
                return_value={
                    "db.example.com": ["dbadmin"],
                    "web.example.com": ["deploy"],
                },
            ),
            patch(
                "remctl.cli.keyring.get_password",
                side_effect=lambda service, username: passwords[username],
            ),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["list"])

        result = output.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("db.example.com", result)
        self.assertIn("dbadmin", result)
        self.assertIn("web.example.com", result)
        self.assertIn("deploy", result)
        self.assertNotIn("db-secret", result)
        self.assertNotIn("web-secret", result)

    def test_list_hosts_reports_empty_index(self) -> None:
        output = io.StringIO()

        with (
            patch("remctl.cli.load_host_index", return_value={}),
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["list"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(output.getvalue(), "No saved hosts.\n")

    def test_delete_requires_username_for_multiple_accounts(self) -> None:
        error = io.StringIO()

        with (
            patch(
                "remctl.cli.load_host_index",
                return_value={"example.com": ["alice", "root"]},
            ),
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(["delete", "example.com"])

        self.assertEqual(exit_code, 2)
        self.assertIn("multiple credentials found", error.getvalue())

    def test_delete_all_removes_every_account_for_host(self) -> None:
        output = io.StringIO()

        with (
            patch(
                "remctl.cli.load_host_index",
                return_value={"example.com": ["alice", "root"]},
            ),
            patch("remctl.cli.keyring.get_password", return_value="secret"),
            patch("remctl.cli.keyring.delete_password") as delete_password,
            patch("remctl.cli.unregister_host") as unregister_host,
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["delete", "example.com", "--all"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(delete_password.call_count, 2)
        delete_password.assert_any_call("remctl:example.com", "alice")
        delete_password.assert_any_call("remctl:example.com", "root")
        self.assertEqual(unregister_host.call_count, 2)

    def test_register_host_keeps_multiple_usernames(self) -> None:
        with (
            patch(
                "remctl.cli.load_host_index",
                return_value={"example.com": ["alice"]},
            ),
            patch("remctl.cli.save_host_index") as save_host_index,
        ):
            from remctl.cli import register_host

            register_host("example.com", "root")

        save_host_index.assert_called_once_with(
            {"example.com": ["alice", "root"]}
        )

    def test_ssh_uses_only_saved_credential(self) -> None:
        with (
            patch(
                "remctl.cli.load_host_index",
                return_value={"10.0.0.11": ["root"]},
            ),
            patch("remctl.cli.keyring.get_password", return_value="secret"),
            patch("remctl.cli.run_ssh", return_value=0) as run_ssh,
        ):
            exit_code = main(["ssh", "10.0.0.11"])

        self.assertEqual(exit_code, 0)
        run_ssh.assert_called_once_with("10.0.0.11", "root", "secret")

    def test_ssh_requires_user_for_multiple_credentials(self) -> None:
        error = io.StringIO()

        with (
            patch(
                "remctl.cli.load_host_index",
                return_value={"10.0.0.11": ["admin", "root"]},
            ),
            patch("remctl.cli.run_ssh") as run_ssh,
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(["ssh", "10.0.0.11"])

        self.assertEqual(exit_code, 2)
        run_ssh.assert_not_called()
        self.assertIn("specify --user", error.getvalue())

    def test_ssh_offers_to_add_missing_credential_and_continues(self) -> None:
        output = io.StringIO()
        with (
            patch("remctl.cli.load_host_index", return_value={}),
            patch(
                "builtins.input", side_effect=["yes", "alice"]
            ) as user_input,
            patch("remctl.cli.getpass.getpass", return_value="secret"),
            patch("remctl.cli.validate_ssh_credential", return_value=True) as validate,
            patch("remctl.cli.save_host_credential") as save,
            patch("remctl.cli.run_ssh", return_value=0) as run_ssh,
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["ssh", "new.example.com"])

        self.assertEqual(exit_code, 0)
        validate.assert_called_once_with(
            "new.example.com", "alice", "secret", debug=False
        )
        save.assert_called_once_with("new.example.com", "alice", "secret")
        run_ssh.assert_called_once_with("new.example.com", "alice", "secret")
        self.assertIn("Add it now", user_input.call_args_list[0].args[0])
        self.assertIn("Credential saved", output.getvalue())

    def test_ssh_does_not_continue_when_new_credential_is_invalid(self) -> None:
        error = io.StringIO()
        with (
            patch("remctl.cli.load_host_index", return_value={}),
            patch("builtins.input", side_effect=["yes", "alice"]),
            patch("remctl.cli.getpass.getpass", return_value="wrong"),
            patch("remctl.cli.validate_ssh_credential", return_value=False),
            patch("remctl.cli.save_host_credential") as save,
            patch("remctl.cli.run_ssh") as run_ssh,
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(["ssh", "new.example.com"])

        self.assertEqual(exit_code, 1)
        save.assert_not_called()
        run_ssh.assert_not_called()
        self.assertIn("validation failed", error.getvalue())

    def test_exec_uses_explicit_username_when_adding_credential(self) -> None:
        output = io.StringIO()
        with (
            patch("remctl.cli.load_host_index", return_value={}),
            patch("remctl.cli.keyring.get_password", return_value=None),
            patch("builtins.input", return_value="yes"),
            patch("remctl.cli.getpass.getpass", return_value="secret"),
            patch("remctl.cli.validate_ssh_credential", return_value=True),
            patch("remctl.cli.save_host_credential") as save,
            patch("remctl.cli.run_ssh", return_value=0) as run_ssh,
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(
                ["exec", "--user", "root", "new.example.com", "hostname"]
            )

        self.assertEqual(exit_code, 0)
        save.assert_called_once_with("new.example.com", "root", "secret")
        run_ssh.assert_called_once_with(
            "new.example.com",
            "root",
            "secret",
            remote_command=("hostname",),
            interactive=False,
        )

    def test_exec_runs_remote_command_and_returns_ssh_exit_code(self) -> None:
        with (
            patch(
                "remctl.cli.load_host_index",
                return_value={"10.0.0.11": ["root"]},
            ),
            patch("remctl.cli.keyring.get_password", return_value="secret"),
            patch("remctl.cli.run_ssh", return_value=7) as run_ssh,
        ):
            exit_code = main(["exec", "10.0.0.11", "uname", "-a"])

        self.assertEqual(exit_code, 7)
        run_ssh.assert_called_once_with(
            "10.0.0.11",
            "root",
            "secret",
            remote_command=("uname", "-a"),
            interactive=False,
        )

    def test_exec_uses_selected_user_for_multiple_credentials(self) -> None:
        with (
            patch(
                "remctl.cli.load_host_index",
                return_value={"10.0.0.11": ["admin", "root"]},
            ),
            patch("remctl.cli.keyring.get_password", return_value="secret"),
            patch("remctl.cli.run_ssh", return_value=0) as run_ssh,
        ):
            exit_code = main(
                ["exec", "--user", "root", "10.0.0.11", "hostname"]
            )

        self.assertEqual(exit_code, 0)
        run_ssh.assert_called_once_with(
            "10.0.0.11",
            "root",
            "secret",
            remote_command=("hostname",),
            interactive=False,
        )

    def test_exec_rejects_empty_remote_command(self) -> None:
        error = io.StringIO()

        with (
            patch("remctl.cli.run_ssh") as run_ssh,
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(["exec", "10.0.0.11"])

        self.assertEqual(exit_code, 2)
        run_ssh.assert_not_called()
        self.assertIn("command cannot be empty", error.getvalue())

    def test_deploy_checks_file_before_credentials(self) -> None:
        error = io.StringIO()

        with (
            patch("remctl.cli.load_host_index") as load_host_index,
            patch("remctl.cli.deploy_many") as deploy_many,
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(["deploy", "missing.bin", "10.0.0.11"])

        self.assertEqual(exit_code, 2)
        load_host_index.assert_not_called()
        deploy_many.assert_not_called()
        self.assertIn("file does not exist", error.getvalue())

    def test_deploy_preflights_credentials_and_runs_multiple_hosts(self) -> None:
        output = io.StringIO()
        selected_workflow = object()
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "archon.jar"
            artifact.write_bytes(b"jar")
            with (
                patch(
                    "remctl.cli.load_host_index",
                    return_value={
                        "10.0.0.11": ["root"],
                        "10.0.0.12": ["root"],
                    },
                ),
                patch("remctl.cli.keyring.get_password", return_value="secret"),
                patch("remctl.cli.load_actions", return_value={}),
                patch(
                    "remctl.cli.load_workflow",
                    return_value=selected_workflow,
                ) as load_workflow,
                patch(
                    "remctl.cli.deploy_many",
                    return_value=[
                        DeployResult("10.0.0.11", True, "deployed"),
                        DeployResult("10.0.0.12", True, "deployed"),
                    ],
                ) as deploy_many,
                contextlib.redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "deploy",
                        "--post-workflow",
                        "deploy-archon",
                        str(artifact),
                        "10.0.0.11",
                        "10.0.0.12",
                    ]
                )

        self.assertEqual(exit_code, 0)
        local_path, targets, resolved_workflow = deploy_many.call_args.args
        self.assertEqual(local_path.name, "archon.jar")
        self.assertEqual(
            [target.host for target in targets],
            ["10.0.0.11", "10.0.0.12"],
        )
        self.assertIs(resolved_workflow, selected_workflow)
        load_workflow.assert_called_once_with("deploy-archon", {})
        self.assertIn("[10.0.0.11] OK", output.getvalue())

    def test_deploy_aborts_before_transfer_when_credential_is_missing(self) -> None:
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "image.tar"
            artifact.write_bytes(b"image")
            with (
                patch("remctl.cli.load_host_index", return_value={}),
                patch("remctl.cli.deploy_many") as deploy_many,
                patch("builtins.input", return_value="no"),
                contextlib.redirect_stderr(error),
            ):
                exit_code = main(["deploy", str(artifact), "10.0.0.11"])

        self.assertEqual(exit_code, 1)
        deploy_many.assert_not_called()
        self.assertIn("no credential found", error.getvalue())

    def test_deploy_collects_and_saves_missing_credentials_before_transfer(
        self,
    ) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "image.tar"
            artifact.write_bytes(b"image")
            with (
                patch("remctl.cli.load_host_index", return_value={}),
                patch(
                    "builtins.input",
                    side_effect=["yes", "alice", "yes", "bob"],
                ),
                patch(
                    "remctl.cli.getpass.getpass",
                    side_effect=["first-secret", "second-secret"],
                ),
                patch("remctl.cli.validate_ssh_credential", return_value=True),
                patch("remctl.cli.save_host_credential") as save,
                patch(
                    "remctl.cli.deploy_many",
                    return_value=[
                        DeployResult("host-one", True, "uploaded"),
                        DeployResult("host-two", True, "uploaded"),
                    ],
                ) as deploy_many,
                contextlib.redirect_stdout(output),
            ):
                exit_code = main(
                    ["deploy", str(artifact), "host-one", "host-two"]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(save.call_count, 2)
        save.assert_any_call("host-one", "alice", "first-secret")
        save.assert_any_call("host-two", "bob", "second-secret")
        targets = deploy_many.call_args.args[1]
        self.assertEqual(
            [(target.host, target.username) for target in targets],
            [("host-one", "alice"), ("host-two", "bob")],
        )

    def test_deploy_rejects_invalid_workflow_before_reading_credentials(self) -> None:
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "image.tar"
            artifact.write_bytes(b"image")
            with (
                patch("remctl.cli.load_actions", return_value={}),
                patch(
                    "remctl.cli.load_workflow",
                    side_effect=WorkflowConfigError("workflow not found: missing"),
                ),
                patch("remctl.cli.load_host_index") as load_host_index,
                patch("remctl.cli.deploy_many") as deploy_many,
                contextlib.redirect_stderr(error),
            ):
                exit_code = main(
                    [
                        "deploy",
                        "--post-workflow",
                        "missing",
                        str(artifact),
                        "10.0.0.11",
                    ]
                )

        self.assertEqual(exit_code, 2)
        load_host_index.assert_not_called()
        deploy_many.assert_not_called()
        self.assertIn("unable to load post-workflow", error.getvalue())

    def test_deploy_rejects_removed_operation_flags(self) -> None:
        with self.assertRaises(SystemExit):
            main(["deploy", "-i", "image.tar", "10.0.0.11"])

    def test_deploy_rejects_post_work_flow_alias(self) -> None:
        with self.assertRaises(SystemExit):
            main(
                [
                    "deploy",
                    "--post-work-flow",
                    "example",
                    "image.tar",
                    "10.0.0.11",
                ]
            )

    def test_scp_push_transfers_to_requested_remote_path_with_progress(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "artifact.jar"
            source.write_bytes(b"jar")

            def run_scp(*args, **kwargs):
                args[5](67, "9.2MB/s")
                return 0, ""

            with (
                patch(
                    "remctl.cli.load_host_index",
                    return_value={"10.0.0.11": ["root"]},
                ),
                patch("remctl.cli.keyring.get_password", return_value="secret"),
                patch("remctl.cli.run_scp", side_effect=run_scp) as scp,
                contextlib.redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "scp",
                        "push",
                        str(source),
                        "10.0.0.11",
                        "/opt/app/",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(scp.call_args.args[4], "/opt/app/")
        self.assertIn("67%", output.getvalue())
        self.assertIn("9.2MB/s", output.getvalue())
        self.assertIn("uploaded", output.getvalue())

    def test_scp_pull_transfers_remote_file_to_local_path(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "server.log"

            def run_scp_pull(*args, **kwargs):
                args[5](35, "2.1MB/s")
                return 0, ""

            with (
                patch(
                    "remctl.cli.load_host_index",
                    return_value={"10.0.0.11": ["root"]},
                ),
                patch("remctl.cli.keyring.get_password", return_value="secret"),
                patch(
                    "remctl.cli.run_scp_pull",
                    side_effect=run_scp_pull,
                ) as scp_pull,
                contextlib.redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "scp",
                        "pull",
                        "10.0.0.11",
                        "/var/log/server.log",
                        str(destination),
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(scp_pull.call_args.args[3], "/var/log/server.log")
        self.assertEqual(scp_pull.call_args.args[4], destination)
        self.assertIn("35%", output.getvalue())
        self.assertIn("2.1MB/s", output.getvalue())
        self.assertIn("downloaded", output.getvalue())

    def test_scp_push_checks_local_source_before_credentials(self) -> None:
        error = io.StringIO()
        with (
            patch("remctl.cli.load_host_index") as load_host_index,
            patch("remctl.cli.run_scp") as run_scp,
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(
                ["scp", "push", "missing.bin", "10.0.0.11", "/tmp/"]
            )

        self.assertEqual(exit_code, 2)
        load_host_index.assert_not_called()
        run_scp.assert_not_called()

    def test_rsync_push_passes_options_and_preserves_trailing_slash(self) -> None:
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "release"
            source.mkdir()

            def run_rsync(*args, **kwargs):
                args[5](72, "4.8MB/s")
                return 0, ""

            with (
                patch(
                    "remctl.cli.load_host_index",
                    return_value={"10.0.0.11": ["root"]},
                ),
                patch("remctl.cli.keyring.get_password", return_value="secret"),
                patch("remctl.cli.run_rsync", side_effect=run_rsync) as rsync,
                contextlib.redirect_stdout(output),
            ):
                exit_code = main(
                    [
                        "rsync",
                        "push",
                        "--compress",
                        "--delete",
                        "--exclude",
                        "*.tmp",
                        "--partial",
                        "--bwlimit",
                        "1024",
                        f"{source}/",
                        "10.0.0.11",
                        "/opt/release/",
                    ]
                )

        self.assertEqual(exit_code, 0)
        self.assertTrue(rsync.call_args.args[0].endswith("/"))
        self.assertTrue(rsync.call_args.kwargs["archive"])
        self.assertTrue(rsync.call_args.kwargs["compress"])
        self.assertTrue(rsync.call_args.kwargs["delete"])
        self.assertTrue(rsync.call_args.kwargs["partial"])
        self.assertEqual(rsync.call_args.kwargs["excludes"], ("*.tmp",))
        self.assertEqual(rsync.call_args.kwargs["bandwidth_limit"], 1024)
        self.assertIn("72%", output.getvalue())

    def test_rsync_rejects_nonpositive_bandwidth_limit(self) -> None:
        with self.assertRaises(SystemExit):
            main(
                [
                    "rsync",
                    "push",
                    "--bwlimit",
                    "0",
                    "source",
                    "10.0.0.11",
                    "/tmp/",
                ]
            )

    def test_reindex_removes_missing_credentials(self) -> None:
        output = io.StringIO()

        def load_credential(host: str, username: str):
            if username == "alice":
                return HostCredential(username="alice", password="secret")
            return None

        with (
            patch(
                "remctl.cli.load_host_index",
                return_value={"example.com": ["alice", "missing"]},
            ),
            patch(
                "remctl.cli.load_host_credential",
                side_effect=load_credential,
            ),
            patch("remctl.cli.save_host_index") as save_host_index,
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["reindex"])

        self.assertEqual(exit_code, 0)
        save_host_index.assert_called_once_with({"example.com": ["alice"]})
        self.assertIn("1 valid, 1 removed", output.getvalue())

    def test_reindex_resets_invalid_index(self) -> None:
        output = io.StringIO()

        with (
            patch(
                "remctl.cli.load_host_index",
                side_effect=ValueError("invalid index"),
            ),
            patch("remctl.cli.save_host_index") as save_host_index,
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["reindex"])

        self.assertEqual(exit_code, 0)
        save_host_index.assert_called_once_with({})
        self.assertIn("has been reset", output.getvalue())

    def test_save_rolls_back_new_index_when_credential_write_fails(self) -> None:
        from remctl.cli import save_host_credential

        with (
            patch("remctl.cli.load_host_index", return_value={}),
            patch("remctl.cli.register_host") as register_host,
            patch(
                "remctl.cli.keyring.set_password",
                side_effect=keyring.errors.PasswordSetError("write failed"),
            ),
            patch("remctl.cli.unregister_host") as unregister_host,
        ):
            with self.assertRaises(keyring.errors.PasswordSetError):
                save_host_credential("example.com", "alice", "secret")

        register_host.assert_called_once_with("example.com", "alice")
        unregister_host.assert_called_once_with("example.com", "alice")

    def test_uninstall_can_keep_user_data(self) -> None:
        output = io.StringIO()
        with (
            patch("builtins.input", side_effect=["yes", "no"]),
            patch(
                "remctl.cli.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ) as run,
            patch("remctl.cli.purge_remctl_data") as purge,
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["uninstall"])

        self.assertEqual(exit_code, 0)
        self.initialize_config.assert_not_called()
        purge.assert_not_called()
        run.assert_called_once_with(
            [
                sys.executable,
                "-m",
                "pip",
                "uninstall",
                "--yes",
                "remctl",
            ],
            check=False,
        )
        self.assertIn("configuration and credentials were kept", output.getvalue())

    def test_uninstall_can_purge_user_data(self) -> None:
        with (
            patch("builtins.input", side_effect=["yes", "yes"]),
            patch(
                "remctl.cli.subprocess.run",
                return_value=SimpleNamespace(returncode=0),
            ),
            patch("remctl.cli.purge_remctl_data", return_value=0) as purge,
        ):
            exit_code = main(["uninstall"])

        self.assertEqual(exit_code, 0)
        purge.assert_called_once_with()

    def test_uninstall_can_be_cancelled(self) -> None:
        output = io.StringIO()
        with (
            patch("builtins.input", return_value="no"),
            patch("remctl.cli.subprocess.run") as run,
            contextlib.redirect_stdout(output),
        ):
            exit_code = main(["uninstall"])

        self.assertEqual(exit_code, 0)
        run.assert_not_called()

    def test_uninstall_does_not_purge_data_when_pip_fails(self) -> None:
        error = io.StringIO()
        with (
            patch("builtins.input", side_effect=["yes", "yes"]),
            patch(
                "remctl.cli.subprocess.run",
                return_value=SimpleNamespace(returncode=1),
            ),
            patch("remctl.cli.purge_remctl_data") as purge,
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(["uninstall"])

        self.assertEqual(exit_code, 1)
        purge.assert_not_called()
        self.assertIn("unable to uninstall", error.getvalue())

    def test_interrupting_user_data_prompt_cancels_uninstall(self) -> None:
        output = io.StringIO()
        error = io.StringIO()
        with (
            patch("builtins.input", side_effect=["yes", KeyboardInterrupt]),
            patch("remctl.cli.subprocess.run") as run,
            contextlib.redirect_stdout(output),
            contextlib.redirect_stderr(error),
        ):
            exit_code = main(["uninstall"])

        self.assertEqual(exit_code, 0)
        run.assert_not_called()
        self.assertIn("cancelled", output.getvalue())

    def test_purge_deletes_yaml_and_indexed_keyring_credentials(self) -> None:
        from remctl.cli import purge_remctl_data

        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            config_dir = Path(directory) / ".remctl"
            config_dir.mkdir()
            actions_path = config_dir / "actions.yaml"
            workflows_path = config_dir / "workflows.yaml"
            unrelated_path = config_dir / "notes.txt"
            actions_path.write_text("actions", encoding="utf-8")
            workflows_path.write_text("workflows", encoding="utf-8")
            unrelated_path.write_text("keep", encoding="utf-8")
            with (
                patch(
                    "remctl.cli.load_host_index",
                    return_value={"example.com": ["alice", "root"]},
                ),
                patch("remctl.cli.keyring.get_password", return_value="secret"),
                patch("remctl.cli.keyring.delete_password") as delete_password,
                patch("remctl.cli.CONFIG_DIR", config_dir),
                patch("remctl.cli.ACTIONS_PATH", actions_path),
                patch("remctl.cli.WORKFLOWS_PATH", workflows_path),
                contextlib.redirect_stdout(output),
            ):
                exit_code = purge_remctl_data()

            self.assertFalse(actions_path.exists())
            self.assertFalse(workflows_path.exists())
            self.assertTrue(unrelated_path.exists())

        self.assertEqual(exit_code, 0)
        delete_password.assert_any_call("remctl:example.com", "alice")
        delete_password.assert_any_call("remctl:example.com", "root")
        delete_password.assert_any_call("remctl:index", "hosts")
        self.assertIn("2 credentials, 2 configuration files", output.getvalue())


if __name__ == "__main__":
    unittest.main()
