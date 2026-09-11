"""Command-line interface for remctl."""

import argparse
import getpass
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass

import keyring

from remctl import __version__
from remctl.ssh import run_ssh, validate_ssh_credential

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


def add_host(host: str) -> int:
    """Prompt for and securely store a host credential."""
    username = input("username: ").strip()
    if not username:
        print("error: username cannot be empty", file=sys.stderr)
        return 2

    password = getpass.getpass("password: ")
    if not password:
        print("error: password cannot be empty", file=sys.stderr)
        return 2

    print(f"Validating SSH credential for {username}@{host}...")
    if not validate_ssh_credential(host, username, password):
        print("error: SSH credential validation failed", file=sys.stderr)
        return 1

    try:
        save_host_credential(host, username, password)
    except (keyring.errors.KeyringError, ValueError) as error:
        print(f"error: unable to save credential: {error}", file=sys.stderr)
        return 1

    print(f"Credential saved for {username}@{host}")
    return 0


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
        usernames = index.get(host, [])
        if username is None:
            if len(usernames) > 1:
                print(
                    "error: multiple credentials found; specify --user",
                    file=sys.stderr,
                )
                return 2
            if len(usernames) == 1:
                username = usernames[0]

        if username is None:
            print(f"error: no credential found for {host}", file=sys.stderr)
            return 1

        credential = load_host_credential(host, username)
    except (keyring.errors.KeyringError, ValueError) as error:
        print(f"error: unable to read credential: {error}", file=sys.stderr)
        return 1

    if credential is None:
        print(f"error: no credential found for {username}@{host}", file=sys.stderr)
        return 1
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
        usernames = index.get(host, [])
        if username is None:
            if len(usernames) > 1:
                print(
                    "error: multiple credentials found; specify --user",
                    file=sys.stderr,
                )
                return 2
            if len(usernames) == 1:
                username = usernames[0]

        if username is None:
            print(f"error: no credential found for {host}", file=sys.stderr)
            return 1

        credential = load_host_credential(host, username)
    except (keyring.errors.KeyringError, ValueError) as error:
        print(f"error: unable to read credential: {error}", file=sys.stderr)
        return 1

    if credential is None:
        print(f"error: no credential found for {username}@{host}", file=sys.stderr)
        return 1
    return run_ssh(
        host,
        credential.username,
        credential.password,
        remote_command=tuple(remote_command),
        interactive=False,
    )


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
    add_parser.set_defaults(handler=lambda args: add_host(args.host))

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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command-line application."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "handler"):
        parser.print_help()
        return 0
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
