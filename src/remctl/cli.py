"""Command-line interface for remctl."""

import argparse
import getpass
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import keyring

from remctl import __version__
from remctl.completion import print_completion
from remctl.deploy import DeployTarget, ProgressDisplay, deploy_many
from remctl.ssh import run_ssh, validate_ssh_credential
from remctl.transfer import run_rsync, run_rsync_pull, run_scp, run_scp_pull
from remctl.workflow import (
    ACTIONS_PATH,
    CONFIG_DIR,
    WORKFLOWS_PATH,
    WorkflowConfigError,
    initialize_config,
    load_actions,
    load_workflow,
)

KEYRING_INDEX_SERVICE = "remctl:index"
KEYRING_INDEX_ACCOUNT = "hosts"


@dataclass(frozen=True)
class HostCredential:
    """A remote host credential loaded from the system keyring."""

    username: str
    password: str


def credential_service(host: str) -> str:
    """Return the keyring service used for a host."""
    return f"remctl:{host}"


def load_host_credential(host: str, username: str) -> HostCredential | None:
    """Load a host credential from the system keyring."""
    password = keyring.get_password(credential_service(host), username)
    if password is None:
        return None
    return HostCredential(username=username, password=password)


def load_host_index() -> dict[str, list[str]]:
    """Load the host and username index from the system keyring."""
    secret = keyring.get_password(KEYRING_INDEX_SERVICE, KEYRING_INDEX_ACCOUNT)
    if secret is None:
        return {}

    try:
        index = json.loads(secret)
    except json.JSONDecodeError as error:
        raise ValueError("stored host index has an invalid format") from error

    if isinstance(index, list):
        raise ValueError("stored host index uses an obsolete format; run rctl reindex")

    if not isinstance(index, dict) or not all(
        isinstance(host, str)
        and host
        and isinstance(usernames, list)
        and all(isinstance(username, str) and username for username in usernames)
        for host, usernames in index.items()
    ):
        raise ValueError("stored host index has an invalid format")
    return {
        host: sorted(set(usernames))
        for host, usernames in sorted(index.items())
        if usernames
    }


def save_host_index(index: dict[str, list[str]]) -> None:
    """Save a normalized host index in the system keyring."""
    normalized = {
        host: sorted(set(usernames))
        for host, usernames in sorted(index.items())
        if usernames
    }
    keyring.set_password(
        KEYRING_INDEX_SERVICE,
        KEYRING_INDEX_ACCOUNT,
        json.dumps(normalized),
    )


def register_host(host: str, username: str) -> None:
    """Add a host and username to the keyring-backed index."""
    index = load_host_index()
    usernames = index.setdefault(host, [])
    if username not in usernames:
        usernames.append(username)
        save_host_index(index)


def unregister_host(host: str, username: str) -> None:
    """Remove a host and username from the keyring-backed index."""
    index = load_host_index()
    usernames = index.get(host, [])
    if username in usernames:
        usernames.remove(username)
        if not usernames:
            index.pop(host)
        save_host_index(index)


def save_host_credential(host: str, username: str, password: str) -> None:
    """Index and save a credential, rolling back a new index entry on failure."""
    index = load_host_index()
    already_indexed = username in index.get(host, [])
    if not already_indexed:
        register_host(host, username)
    try:
        keyring.set_password(credential_service(host), username, password)
    except keyring.errors.KeyringError:
        if not already_indexed:
            unregister_host(host, username)
        raise


def delete_host_credential(host: str, username: str, password: str) -> None:
    """Delete a credential and restore it if index updating fails."""
    service = credential_service(host)
    keyring.delete_password(service, username)
    try:
        unregister_host(host, username)
    except (keyring.errors.KeyringError, ValueError):
        keyring.set_password(service, username, password)
        raise


def _prompt_and_save_credential(
    host: str,
    username: str | None = None,
    *,
    debug: bool = False,
) -> tuple[HostCredential | None, int]:
    """Prompt for, validate, and save one credential."""
    try:
        if username is None:
            username = input("username: ").strip()
    except KeyboardInterrupt:
        print("\nCredential entry interrupted.", file=sys.stderr)
        return None, 130
    except EOFError:
        print("error: unable to read credential", file=sys.stderr)
        return None, 2

    if not username:
        print("error: username cannot be empty", file=sys.stderr)
        return None, 2

    try:
        password = getpass.getpass("password: ")
    except KeyboardInterrupt:
        print("\nCredential entry interrupted.", file=sys.stderr)
        return None, 130
    except EOFError:
        print("error: unable to read credential", file=sys.stderr)
        return None, 2

    if not password:
        print("error: password cannot be empty", file=sys.stderr)
        return None, 2

    if debug:
        print(f"Validating SSH credential for {username}@{host}...")
    if not validate_ssh_credential(host, username, password, debug=debug):
        print("error: SSH credential validation failed", file=sys.stderr)
        return None, 1

    try:
        save_host_credential(host, username, password)
    except (keyring.errors.KeyringError, ValueError) as error:
        print(f"error: unable to save credential: {error}", file=sys.stderr)
        return None, 1

    print(f"Credential saved for {username}@{host}")
    return HostCredential(username, password), 0


def add_host(host: str, *, debug: bool = False) -> int:
    """Prompt for and securely store a host credential."""
    _, exit_code = _prompt_and_save_credential(host, debug=debug)
    return exit_code


def resolve_host_credential(
    host: str,
    index: dict[str, list[str]],
    username: str | None = None,
) -> tuple[HostCredential | None, int]:
    """Resolve a credential, offering to add it when none is stored."""
    usernames = index.get(host, [])
    if username is None:
        if len(usernames) > 1:
            print(
                f"error: multiple credentials found for {host}; specify --user",
                file=sys.stderr,
            )
            return None, 2
        if len(usernames) == 1:
            username = usernames[0]

    credential = (
        load_host_credential(host, username) if username is not None else None
    )
    if credential is not None:
        return credential, 0

    identity = f"{username}@{host}" if username is not None else host
    confirmed = _confirm(
        f"No saved credential for {identity}. Add it now? [y/N]: "
    )
    if confirmed is None:
        return None, 130
    if not confirmed:
        print(f"error: no credential found for {identity}", file=sys.stderr)
        return None, 1
    return _prompt_and_save_credential(host, username)


def reindex_hosts() -> int:
    """Remove stale index entries and reset an invalid index."""
    try:
        try:
            index = load_host_index()
        except ValueError:
            save_host_index({})
            print("Host index was invalid and has been reset.")
            return 0

        repaired: dict[str, list[str]] = {}
        removed = 0
        for host, usernames in index.items():
            for username in usernames:
                credential = load_host_credential(host, username)
                if credential is None:
                    removed += 1
                    continue
                if not credential.password:
                    keyring.delete_password(credential_service(host), username)
                    removed += 1
                    continue
                repaired.setdefault(host, []).append(username)

        if repaired != index:
            save_host_index(repaired)
    except keyring.errors.KeyringError as error:
        print(f"error: unable to repair host index: {error}", file=sys.stderr)
        return 1

    valid = sum(len(usernames) for usernames in repaired.values())
    if removed:
        print(f"Host index repaired: {valid} valid, {removed} removed.")
    else:
        print(f"Host index is consistent: {valid} credentials.")
    return 0


def ssh_host(host: str, username: str | None = None) -> int:
    """Open an interactive SSH session using a stored credential."""
    try:
        index = load_host_index()
        credential, exit_code = resolve_host_credential(host, index, username)
    except (keyring.errors.KeyringError, ValueError) as error:
        print(f"error: unable to read credential: {error}", file=sys.stderr)
        return 1

    if credential is None:
        return exit_code
    return run_ssh(host, credential.username, credential.password)


def exec_host(
    host: str,
    command: Sequence[str],
    username: str | None = None,
) -> int:
    """Execute a remote command using a stored credential."""
    remote_command = list(command)
    if remote_command[:1] == ["--"]:
        remote_command.pop(0)
    if not remote_command:
        print("error: remote command cannot be empty", file=sys.stderr)
        return 2

    try:
        index = load_host_index()
        credential, exit_code = resolve_host_credential(host, index, username)
    except (keyring.errors.KeyringError, ValueError) as error:
        print(f"error: unable to read credential: {error}", file=sys.stderr)
        return 1

    if credential is None:
        return exit_code
    return run_ssh(
        host,
        credential.username,
        credential.password,
        remote_command=tuple(remote_command),
        interactive=False,
    )


def deploy_hosts(
    file_path: str,
    hosts: Sequence[str],
    workflow_name: str | None = None,
    username: str | None = None,
) -> int:
    """Validate deployment inputs and deploy to all requested hosts."""
    local_path = Path(file_path).expanduser()
    if not local_path.is_file():
        print(f"error: file does not exist: {local_path}", file=sys.stderr)
        return 2

    workflow = None
    if workflow_name is not None:
        try:
            workflow = load_workflow(workflow_name, load_actions())
        except WorkflowConfigError as error:
            print(f"error: unable to load post-workflow: {error}", file=sys.stderr)
            return 2

    unique_hosts = list(dict.fromkeys(hosts))
    targets: list[DeployTarget] = []
    try:
        index = load_host_index()
        for host in unique_hosts:
            credential, exit_code = resolve_host_credential(host, index, username)
            if credential is None:
                return exit_code
            targets.append(
                DeployTarget(host, credential.username, credential.password)
            )
    except (keyring.errors.KeyringError, ValueError) as error:
        print(f"error: unable to read credential: {error}", file=sys.stderr)
        return 1

    results = deploy_many(local_path, targets, workflow)
    failed = False
    print("\nDeployment results:")
    for result in results:
        status = "OK" if result.success else "FAILED"
        print(f"[{result.host}] {status}: {result.summary}")
        for line in result.output.strip().splitlines():
            print(f"[{result.host}] {line}")
        failed = failed or not result.success
    return 1 if failed else 0


def scp_transfer(
    direction: str,
    host: str,
    source: str,
    destination: str,
    username: str | None = None,
    *,
    recursive: bool = False,
) -> int:
    """Push or pull a path with a stored host credential."""
    local_path = Path(source if direction == "push" else destination).expanduser()
    if direction == "push" and not local_path.exists():
        print(f"error: local path does not exist: {local_path}", file=sys.stderr)
        return 2
    if direction == "pull" and not local_path.parent.is_dir():
        print(
            f"error: local destination directory does not exist: {local_path.parent}",
            file=sys.stderr,
        )
        return 2

    try:
        index = load_host_index()
        credential, exit_code = resolve_host_credential(host, index, username)
    except (keyring.errors.KeyringError, ValueError) as error:
        print(f"error: unable to read credential: {error}", file=sys.stderr)
        return 1

    if credential is None:
        return exit_code

    display = ProgressDisplay([host])
    phase = "pushing" if direction == "push" else "pulling"
    display.update(host, phase, 0, "0B/s")

    def progress(percent: int, rate: str) -> None:
        display.update(host, phase, percent, rate)

    if direction == "push":
        exit_code, transcript = run_scp(
            local_path,
            host,
            credential.username,
            credential.password,
            destination,
            progress,
            recursive=recursive or local_path.is_dir(),
        )
    else:
        exit_code, transcript = run_scp_pull(
            host,
            credential.username,
            credential.password,
            source,
            local_path,
            progress,
            recursive=recursive,
        )

    if exit_code != 0:
        display.update(host, "transfer failed")
        print(f"error: scp {direction} failed for {host}", file=sys.stderr)
        for line in transcript.strip().splitlines():
            print(f"[{host}] {line}", file=sys.stderr)
        return exit_code

    display.update(host, "completed", 100)
    if direction == "push":
        print(f"[{host}] uploaded {local_path} to {destination}")
    else:
        print(f"[{host}] downloaded {source} to {local_path}")
    return 0


def _expand_rsync_local_path(value: str) -> str:
    """Expand a local path while preserving rsync's trailing-slash semantics."""
    expanded = str(Path(value).expanduser())
    if value.endswith("/") and not expanded.endswith("/"):
        expanded += "/"
    return expanded


def rsync_transfer(
    direction: str,
    host: str,
    source: str,
    destination: str,
    username: str | None = None,
    *,
    archive: bool = True,
    compress: bool = False,
    delete: bool = False,
    excludes: Sequence[str] = (),
    dry_run: bool = False,
    checksum: bool = False,
    partial: bool = False,
    bandwidth_limit: int | None = None,
) -> int:
    """Push or pull a path with rsync and a stored host credential."""
    local_value = source if direction == "push" else destination
    local_path = Path(local_value).expanduser()
    if direction == "push" and not local_path.exists():
        print(f"error: local path does not exist: {local_path}", file=sys.stderr)
        return 2
    if direction == "pull" and not local_path.parent.is_dir():
        print(
            f"error: local destination directory does not exist: {local_path.parent}",
            file=sys.stderr,
        )
        return 2

    try:
        index = load_host_index()
        credential, exit_code = resolve_host_credential(host, index, username)
    except (keyring.errors.KeyringError, ValueError) as error:
        print(f"error: unable to read credential: {error}", file=sys.stderr)
        return 1

    if credential is None:
        return exit_code

    display = ProgressDisplay([host])
    phase = "pushing" if direction == "push" else "pulling"
    display.update(host, phase, 0, "0B/s")

    def progress(percent: int, rate: str) -> None:
        display.update(host, phase, percent, rate)

    options = {
        "archive": archive,
        "compress": compress,
        "delete": delete,
        "excludes": tuple(excludes),
        "dry_run": dry_run,
        "checksum": checksum,
        "partial": partial,
        "bandwidth_limit": bandwidth_limit,
    }
    if direction == "push":
        exit_code, transcript = run_rsync(
            _expand_rsync_local_path(source),
            host,
            credential.username,
            credential.password,
            destination,
            progress,
            **options,
        )
    else:
        exit_code, transcript = run_rsync_pull(
            host,
            credential.username,
            credential.password,
            source,
            _expand_rsync_local_path(destination),
            progress,
            **options,
        )

    if exit_code != 0:
        display.update(host, "transfer failed")
        print(f"error: rsync {direction} failed for {host}", file=sys.stderr)
        for line in transcript.strip().splitlines():
            print(f"[{host}] {line}", file=sys.stderr)
        return exit_code

    display.update(host, "completed", 100)
    print(f"[{host}] synchronized {source} to {destination}")
    return 0


def get_host(host: str, username: str | None = None) -> int:
    """Show credentials stored for a host without revealing passwords."""
    try:
        index = load_host_index()
        usernames = [username] if username else index.get(host, [])
        credentials = [
            credential
            for indexed_username in usernames
            if (credential := load_host_credential(host, indexed_username)) is not None
        ]
    except (keyring.errors.KeyringError, ValueError) as error:
        print(f"error: unable to read credential: {error}", file=sys.stderr)
        return 1

    if not credentials:
        print(f"error: no credential found for {host}", file=sys.stderr)
        return 1

    for credential in credentials:
        print(f"host: {host}")
        print(f"username: {credential.username}")
        print("password: stored")
    return 0


def list_hosts() -> int:
    """List credentials tracked by remctl without revealing passwords."""
    try:
        hosts = load_host_index()
        credentials = [
            (host, credential)
            for host, usernames in hosts.items()
            for username in usernames
            if (credential := load_host_credential(host, username)) is not None
        ]
    except (keyring.errors.KeyringError, ValueError) as error:
        print(f"error: unable to list credentials: {error}", file=sys.stderr)
        return 1

    if not credentials:
        print("No saved hosts.")
        return 0

    host_width = max(len("HOST"), *(len(host) for host, _ in credentials))
    print(f"{'HOST':<{host_width}}  USERNAME")
    for host, credential in credentials:
        print(f"{host:<{host_width}}  {credential.username}")
    return 0


def delete_host(
    host: str,
    username: str | None = None,
    *,
    delete_all: bool = False,
) -> int:
    """Delete one or all credentials stored for a host."""
    try:
        if username and delete_all:
            print("error: username and --all cannot be used together", file=sys.stderr)
            return 2

        index = load_host_index()
        indexed_usernames = index.get(host, [])
        if username:
            usernames = [username]
        elif delete_all:
            usernames = indexed_usernames
        elif len(indexed_usernames) == 1:
            usernames = indexed_usernames
        elif len(indexed_usernames) > 1:
            print(
                "error: multiple credentials found; specify username or use --all",
                file=sys.stderr,
            )
            return 2
        else:
            print(f"error: no credential found for {host}", file=sys.stderr)
            return 1

        deleted: list[HostCredential] = []
        for target_username in usernames:
            credential = load_host_credential(host, target_username)
            if credential is None:
                continue
            delete_host_credential(
                host,
                target_username,
                credential.password,
            )
            deleted.append(credential)
    except (keyring.errors.KeyringError, ValueError) as error:
        print(f"error: unable to delete credential: {error}", file=sys.stderr)
        return 1

    if not deleted:
        print(f"error: no credential found for {host}", file=sys.stderr)
        return 1
    for credential in deleted:
        print(f"Credential deleted for {credential.username}@{host}")
    return 0


def _confirm(prompt: str) -> bool | None:
    """Return confirmation, or ``None`` when prompting was interrupted."""
    try:
        return input(prompt).strip().lower() in {"y", "yes"}
    except (EOFError, KeyboardInterrupt):
        print("\nOperation cancelled.", file=sys.stderr)
        return None


def purge_remctl_data() -> int:
    """Delete indexed credentials and remctl YAML configuration files."""
    deleted_credentials = 0
    try:
        index = load_host_index()
        for host, usernames in index.items():
            for username in usernames:
                service = credential_service(host)
                if keyring.get_password(service, username) is not None:
                    keyring.delete_password(service, username)
                    deleted_credentials += 1

        if keyring.get_password(
            KEYRING_INDEX_SERVICE,
            KEYRING_INDEX_ACCOUNT,
        ) is not None:
            keyring.delete_password(
                KEYRING_INDEX_SERVICE,
                KEYRING_INDEX_ACCOUNT,
            )
    except (keyring.errors.KeyringError, ValueError) as error:
        print(f"error: unable to delete Keyring data: {error}", file=sys.stderr)
        return 1

    deleted_files = 0
    try:
        for path in (ACTIONS_PATH, WORKFLOWS_PATH):
            if path.exists():
                path.unlink()
                deleted_files += 1
        try:
            CONFIG_DIR.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # Preserve the directory when it contains unrelated files.
            pass
    except OSError as error:
        print(f"error: unable to delete remctl configuration: {error}", file=sys.stderr)
        return 1

    print(
        "Deleted remctl user data: "
        f"{deleted_credentials} credentials, {deleted_files} configuration files."
    )
    return 0


def uninstall_remctl() -> int:
    """Uninstall this package and optionally remove its stored user data."""
    uninstall = _confirm("Uninstall remctl from this Python environment? [y/N]: ")
    if uninstall is not True:
        print("Uninstall cancelled.")
        return 0

    purge_data = _confirm(
        "Also delete all remctl YAML configuration and indexed Keyring "
        "credentials? [y/N]: "
    )
    if purge_data is None:
        print("Uninstall cancelled.")
        return 0
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "--yes", "remctl"],
            check=False,
        )
    except OSError as error:
        print(f"error: unable to run pip uninstall: {error}", file=sys.stderr)
        return 1
    if result.returncode != 0:
        print("error: pip was unable to uninstall remctl", file=sys.stderr)
        return result.returncode

    if purge_data:
        return purge_remctl_data()
    print("remctl was uninstalled; user configuration and credentials were kept.")
    return 0


def _positive_integer(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _add_rsync_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-u", "--user", help="username to use")
    parser.add_argument(
        "--archive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="preserve metadata and copy recursively (default: enabled)",
    )
    parser.add_argument(
        "-z", "--compress", action="store_true", help="compress transferred data"
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="delete destination files absent from the source",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="exclude a pattern; may be supplied more than once",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="show changes without applying them"
    )
    parser.add_argument(
        "--checksum",
        action="store_true",
        help="compare file checksums instead of size and modification time",
    )
    parser.add_argument(
        "--partial",
        action="store_true",
        help="keep partially transferred files for resuming",
    )
    parser.add_argument(
        "--bwlimit",
        type=_positive_integer,
        metavar="KBPS",
        help="limit transfer bandwidth in KiB per second",
    )


def _handle_rsync_arguments(args: argparse.Namespace, direction: str) -> int:
    return rsync_transfer(
        direction,
        args.host,
        args.source,
        args.destination,
        args.user,
        archive=args.archive,
        compress=args.compress,
        delete=args.delete,
        excludes=args.exclude,
        dry_run=args.dry_run,
        checksum=args.checksum,
        partial=args.partial,
        bandwidth_limit=args.bwlimit,
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="rctl",
        description="Manage remote hosts and run SSH-based operations.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command")
    add_parser = subparsers.add_parser(
        "add",
        help="save credentials for a remote host",
    )
    add_parser.add_argument("host", help="hostname or IP address")
    add_parser.add_argument(
        "--debug",
        action="store_true",
        help="show OpenSSH credential validation diagnostics",
    )
    add_parser.set_defaults(handler=lambda args: add_host(args.host, debug=args.debug))

    get_parser = subparsers.add_parser(
        "get",
        help="show credential information for a remote host",
    )
    get_parser.add_argument("host", help="hostname or IP address")
    get_parser.add_argument("username", nargs="?", help="optional username")
    get_parser.set_defaults(handler=lambda args: get_host(args.host, args.username))

    list_parser = subparsers.add_parser(
        "list",
        aliases=["ls"],
        help="list all saved remote hosts",
    )
    list_parser.set_defaults(handler=lambda args: list_hosts())

    reindex_parser = subparsers.add_parser(
        "reindex",
        help="check and repair the host credential index",
    )
    reindex_parser.set_defaults(handler=lambda args: reindex_hosts())

    ssh_parser = subparsers.add_parser(
        "ssh",
        help="open an SSH session using a saved credential",
    )
    ssh_parser.add_argument("host", help="hostname or IP address")
    ssh_parser.add_argument("-u", "--user", help="username to use")
    ssh_parser.set_defaults(handler=lambda args: ssh_host(args.host, args.user))

    exec_parser = subparsers.add_parser(
        "exec",
        help="execute a command on a remote host",
    )
    exec_parser.add_argument("-u", "--user", help="username to use")
    exec_parser.add_argument("host", help="hostname or IP address")
    exec_parser.add_argument(
        "remote_command",
        nargs=argparse.REMAINDER,
        help="command and arguments to execute remotely",
    )
    exec_parser.set_defaults(
        handler=lambda args: exec_host(
            args.host,
            args.remote_command,
            args.user,
        )
    )

    deploy_parser = subparsers.add_parser(
        "deploy",
        help="transfer and deploy a file to one or more hosts",
    )
    deploy_parser.add_argument("-u", "--user", help="username to use on all hosts")
    deploy_parser.add_argument(
        "--post-workflow",
        help="workflow to run after a successful transfer",
    )
    deploy_parser.add_argument("file", help="local file to transfer")
    deploy_parser.add_argument("hosts", nargs="+", help="target hosts")
    deploy_parser.set_defaults(
        handler=lambda args: deploy_hosts(
            args.file,
            args.hosts,
            args.post_workflow,
            args.user,
        )
    )

    scp_parser = subparsers.add_parser(
        "scp",
        help="push or pull files with a saved credential",
    )
    scp_subparsers = scp_parser.add_subparsers(dest="scp_command", required=True)

    push_parser = scp_subparsers.add_parser("push", help="upload a local path")
    push_parser.add_argument("-u", "--user", help="username to use")
    push_parser.add_argument("source", help="local source file or directory")
    push_parser.add_argument("host", help="remote hostname or IP address")
    push_parser.add_argument("destination", help="remote destination path")
    push_parser.set_defaults(
        handler=lambda args: scp_transfer(
            "push",
            args.host,
            args.source,
            args.destination,
            args.user,
        )
    )

    pull_parser = scp_subparsers.add_parser("pull", help="download a remote path")
    pull_parser.add_argument("-u", "--user", help="username to use")
    pull_parser.add_argument(
        "-r",
        "--recursive",
        action="store_true",
        help="download a directory recursively",
    )
    pull_parser.add_argument("host", help="remote hostname or IP address")
    pull_parser.add_argument("source", help="remote source file or directory")
    pull_parser.add_argument("destination", help="local destination path")
    pull_parser.set_defaults(
        handler=lambda args: scp_transfer(
            "pull",
            args.host,
            args.source,
            args.destination,
            args.user,
            recursive=args.recursive,
        )
    )

    rsync_parser = subparsers.add_parser(
        "rsync",
        help="synchronize files over SSH with a saved credential",
    )
    rsync_subparsers = rsync_parser.add_subparsers(
        dest="rsync_command", required=True
    )

    rsync_push_parser = rsync_subparsers.add_parser(
        "push", help="synchronize a local path to a remote host"
    )
    _add_rsync_options(rsync_push_parser)
    rsync_push_parser.add_argument("source", help="local source path")
    rsync_push_parser.add_argument("host", help="remote hostname or IP address")
    rsync_push_parser.add_argument("destination", help="remote destination path")
    rsync_push_parser.set_defaults(
        handler=lambda args: _handle_rsync_arguments(args, "push")
    )

    rsync_pull_parser = rsync_subparsers.add_parser(
        "pull", help="synchronize a remote path to the local filesystem"
    )
    _add_rsync_options(rsync_pull_parser)
    rsync_pull_parser.add_argument("host", help="remote hostname or IP address")
    rsync_pull_parser.add_argument("source", help="remote source path")
    rsync_pull_parser.add_argument("destination", help="local destination path")
    rsync_pull_parser.set_defaults(
        handler=lambda args: _handle_rsync_arguments(args, "pull")
    )

    delete_parser = subparsers.add_parser(
        "delete",
        aliases=["remove", "rm", "del"],
        help="delete credentials for a remote host",
    )
    delete_parser.add_argument("host", help="hostname or IP address")
    delete_parser.add_argument("username", nargs="?", help="username to delete")
    delete_parser.add_argument(
        "--all",
        action="store_true",
        help="delete every username stored for the host",
    )
    delete_parser.set_defaults(
        handler=lambda args: delete_host(
            args.host,
            args.username,
            delete_all=args.all,
        )
    )

    uninstall_parser = subparsers.add_parser(
        "uninstall",
        help="uninstall remctl and optionally delete its stored user data",
    )
    uninstall_parser.set_defaults(handler=lambda args: uninstall_remctl())

    completion_parser = subparsers.add_parser(
        "completion",
        help="print a shell completion script",
    )
    completion_parser.add_argument("shell", choices=("bash",))
    completion_parser.set_defaults(
        handler=lambda args: print_completion(args.shell)
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line application."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    if args.command not in {"uninstall", "completion"}:
        try:
            initialize_config()
        except OSError as error:
            print(
                f"error: unable to initialize remctl configuration: {error}",
                file=sys.stderr,
            )
            return 1
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
