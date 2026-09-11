# remctl

`remctl` is a Python package for managing remote host credentials through the
operating system Keychain/Keyring and providing unified SSH, SCP, and rsync
operations. Its command-line program is `rctl`.

## Features

- Save multiple username/password credentials for the same remote host.
- Query a stored host without revealing its password.
- List all hosts managed by `remctl` without revealing passwords.
- Delete a host's credentials from the system credential store.
- Validate credentials with a real SSH login before saving them.
- Open interactive SSH sessions with saved credentials.
- Use macOS Keychain through Python's `keyring` package.
- Keep passwords out of project files and shell command history.

SCP and rsync commands will be added in later versions.

## Add a host

Activate the virtual environment, then save credentials for a hostname or IP
address:

```sh
source .venv/bin/activate
rctl add server.example.com
```

The command prompts interactively:

```text
username: alice
password:
Validating SSH credential for alice@server.example.com...
Credential saved for alice@server.example.com
```

The password input is hidden. Each host uses the Keychain/Keyring service name
`remctl:<host>`, with the username as its account and the password as its secret.
Run `rctl add` again with another username to add a credential without
overwriting the existing account.

Before saving, `rctl add` opens an authentication-only OpenSSH connection. It
does not start a remote shell or execute a remote command. A failed login is not
stored. On a first connection, review and confirm the host fingerprint shown by
OpenSSH.

## Open an SSH session

Connect using the only credential stored for a host:

```sh
rctl ssh 10.0.0.11
```

If the host has multiple accounts, select one explicitly:

```sh
rctl ssh 10.0.0.11 --user root
```

The password is read from Keychain/Keyring and sent directly to the OpenSSH
pseudo-terminal. It is never added to process arguments or printed.

If OpenSSH reports that the host key has changed, `rctl` asks before running
`ssh-keygen -R <host>` to remove the stale entry from `~/.ssh/known_hosts`. It
then retries and lets OpenSSH display the new fingerprint for confirmation.
Never approve a changed key until its new fingerprint has been verified through
a trusted channel.

## Query a host

Check whether credentials exist and show the stored username:

```sh
rctl get server.example.com
rctl get server.example.com alice
```

Example output:

```text
host: server.example.com
username: alice
password: stored
```

Without a username, every account saved for that host is shown. Pass a username
to query one account. For security, `rctl get` never prints stored passwords.

## List hosts

List every credential tracked by `remctl`:

```sh
rctl list
```

Example output:

```text
HOST                USERNAME
db.example.com      dbadmin
server.example.com  alice
server.example.com  root
```

The `ls` alias is also available. Passwords are never included in list output.
The host index is stored in Keychain/Keyring under the `remctl:index` service,
not in a plaintext project file.

Check the index against Keychain/Keyring and remove stale entries:

```sh
rctl reindex
```

An invalid index is reset. An index entry whose password no longer exists is
removed, and an empty invalid credential is deleted. Credential and index
updates use repairable ordering and best-effort rollback to prevent new
inconsistencies.
Because the cross-platform `keyring` API cannot enumerate arbitrary entries,
`reindex` cannot discover credentials that were created outside the index.

Credentials created by an earlier development version used a different storage
layout and cannot be discovered automatically. Run `rctl reindex` once to reset
the obsolete index, then run `rctl add <host>` again for each account.

## Delete a host

Remove a host's credentials from Keychain/Keyring:

```sh
rctl delete server.example.com alice
```

`remove`, `rm`, and `del` are also accepted as aliases:

```sh
rctl rm server.example.com
```

If a host has only one account, the username may be omitted. If it has multiple
accounts, specify one username or explicitly delete all accounts:

```sh
rctl delete server.example.com --all
```

## Development

Create a virtual environment and install the project:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --editable .
```

Run the command:

```sh
rctl --version
python -m remctl --version
```

Run the tests with Python's standard library:

```sh
python -m unittest discover -s tests
```
