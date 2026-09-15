"""Tests for the remctl command-line interface."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import keyring

from remctl.cli import HostCredential, main
from remctl.deploy import DeployOperation, DeployResult


class CliTests(unittest.TestCase):
    def test_no_arguments_prints_help(self) -> None:
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            exit_code = main([])

        self.assertEqual(exit_code, 0)
        self.assertIn("usage: rctl", output.getvalue())

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
            exit_code = main(["deploy", "-f", "missing.bin", "10.0.0.11"])

        self.assertEqual(exit_code, 2)
        load_host_index.assert_not_called()
        deploy_many.assert_not_called()
        self.assertIn("file does not exist", error.getvalue())

    def test_deploy_preflights_credentials_and_runs_multiple_hosts(self) -> None:
        output = io.StringIO()
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
                        str(artifact),
                        "10.0.0.11",
                        "10.0.0.12",
                        "-a",
                    ]
                )

        self.assertEqual(exit_code, 0)
        local_path, targets, operation = deploy_many.call_args.args
        self.assertEqual(local_path.name, "archon.jar")
        self.assertEqual(
            [target.host for target in targets],
            ["10.0.0.11", "10.0.0.12"],
        )
        self.assertIs(operation, DeployOperation.ARCHON)
        self.assertIn("[10.0.0.11] OK", output.getvalue())

    def test_deploy_aborts_before_transfer_when_credential_is_missing(self) -> None:
        error = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "image.tar"
            artifact.write_bytes(b"image")
            with (
                patch("remctl.cli.load_host_index", return_value={}),
                patch("remctl.cli.deploy_many") as deploy_many,
                contextlib.redirect_stderr(error),
            ):
                exit_code = main(
                    ["deploy", "-i", str(artifact), "10.0.0.11"]
                )

        self.assertEqual(exit_code, 1)
        deploy_many.assert_not_called()
        self.assertIn("no credential found", error.getvalue())

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


if __name__ == "__main__":
    unittest.main()
