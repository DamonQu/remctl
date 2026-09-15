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
- Push and pull files with SCP, including live progress and bandwidth.
- Deploy files and service artifacts to multiple hosts concurrently.
- Use macOS Keychain through Python's `keyring` package.
- Keep passwords out of project files and shell command history.

Standalone rsync commands will be added in a later version.

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

Before saving, `rctl add` opens an authentication-only OpenSSH connection. It
does not start a remote shell or execute a remote command. A failed login is not
stored. On a first connection, review and confirm the host fingerprint shown by
OpenSSH.

Credential validation is quiet by default: only the final success or failure is
shown. Enable OpenSSH diagnostics when troubleshooting:

```sh
rctl add server.example.com --debug
```

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

## Execute a remote command

Run a command with the only credential stored for a host:

```sh
rctl exec 10.0.0.11 uname -a
```

Quote commands that contain remote shell operators so the local shell does not
interpret them first:

```sh
rctl exec 10.0.0.11 'uptime && df -h'
```

Remote stdout and stderr are printed directly, and `rctl` exits with the SSH
process exit code. For a host with multiple accounts, put `--user` before the
host:

```sh
rctl exec --user root 10.0.0.11 systemctl status sshd
```

Use `--` when the remote command itself begins with an option:

```sh
rctl exec 10.0.0.11 -- -example-command
```

The command is passed as arguments to OpenSSH; the saved password is never
included in the process arguments.

## Transfer files with SCP

Upload a local file to a specific remote path:

```sh
rctl scp push ./artifact.jar 10.0.0.11 /tmp/artifact.jar
```

Local directories are detected automatically and uploaded recursively:

```sh
rctl scp push ./release 10.0.0.11 /opt/releases/
```

Download a remote file to a local path:

```sh
rctl scp pull 10.0.0.11 /var/log/hcdadmin.log ./hcdadmin.log
```

Download a remote directory recursively:

```sh
rctl scp pull --recursive 10.0.0.11 /var/log/hcdadmin ./logs
```

For a host with multiple credentials, select one explicitly:

```sh
rctl scp push --user root ./artifact.jar 10.0.0.11 /tmp/
rctl scp pull --user root 10.0.0.11 /tmp/result.txt ./result.txt
```

Both directions display continuously updated transfer percentage and bandwidth:

```text
[10.0.0.11] pushing  67% 9.2MB/s
[10.0.0.11] completed 100%
```

SCP uses the same Keychain credential and host-key verification behavior as
`rctl ssh`. Passwords are sent through the pseudo-terminal and never placed in
the SCP process arguments.

## Deploy files and artifacts

`rctl deploy` checks that the local file and all target credentials exist before
starting. Targets run concurrently, while the terminal displays each host's
current step, transfer percentage, and bandwidth.

Upload and load a Docker image archive, then remove the temporary archive:

```sh
rctl deploy -i image.tar 10.0.0.11 10.0.0.12
```

The result includes every `Loaded image:` or `Loaded image ID:` reference
reported by `docker image load --input`.

Deploy the Archon jar and restart `hcdadmin`:

```sh
rctl deploy -a archon.jar 10.0.0.11 10.0.0.12
```

The jar is copied to:

```text
/usr/share/hcdserver/hcdadmin/archon-1.0-SNAPSHOT-jar-with-dependencies.jar
```

Deploy the hcdmgmt jar and restart `hcdmgmt`:

```sh
rctl deploy -m hcdmgmt.jar 10.0.0.11 10.0.0.12
```

The jar is copied to:

```text
/usr/share/hcdserver/hcdmgmt/hcdmgmt-1.0-SNAPSHOT.jar
```

Only upload a file and retain it in the generated remote temporary path:

```sh
rctl deploy -f support-bundle.tar.gz 10.0.0.11 10.0.0.12
```

For hosts with multiple credentials, select the same username on all targets:

```sh
rctl deploy --user root -a archon.jar 10.0.0.11 10.0.0.12
```

Operation flags are mutually exclusive:

- `-i`, `--image`: load a Docker image archive.
- `-a`, `--archon`: install the Archon jar and restart `hcdadmin`.
- `-m`, `--hcdmgmt`: install the hcdmgmt jar and restart `hcdmgmt`.
- `-f`, `--file-only`: upload only and print the temporary path.

Docker and jar deployments clean up the remote temporary file. File-only mode
keeps it. Deployment commands require the selected remote account to have
permission to run `docker`, copy into `/usr/share/hcdserver`, and restart the
relevant systemd service.

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
