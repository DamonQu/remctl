# remctl

`remctl` is a Python package for managing remote host credentials through the
operating system Keychain/Keyring and providing unified SSH, SCP, and rsync
operations. Its command-line program is `rctl`.

## Features

- Save multiple username/password credentials for the same remote host.
- Query a stored host without revealing its password.
- List all hosts managed by `remctl` without revealing passwords.
- Delete a host's credentials from the system credential store.
- Use macOS Keychain through Python's `keyring` package.
- Keep passwords out of project files and shell command history.

SSH, SCP, and rsync commands will be added in later versions.

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
Credential saved for alice@server.example.com
```

The password input is hidden. Each host uses the Keychain/Keyring service name
`remctl:<host>`, with the username as its account and the password as its secret.
Run `rctl add` again with another username to add a credential without
overwriting the existing account.

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

Credentials created by an earlier development version used a different storage
layout and cannot be discovered automatically. Run `rctl add <host>` again for
each account to store it in the multi-user layout.

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
