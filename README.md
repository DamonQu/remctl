# remctl

`remctl` is a Python command-line tool for storing remote-host credentials in
the operating system Keychain/Keyring and using them for SSH, SCP, and
multi-host deployment operations. The installed command is `rctl`.

## Features

- Store multiple username/password credentials for one host.
- Validate credentials with a real SSH login before saving them.
- Open interactive SSH sessions and execute remote commands.
- Push and pull files or directories with SCP progress reporting.
- Synchronize files in either direction with rsync over SSH.
- Upload files concurrently and run configurable post-transfer workflows.
- Keep passwords out of project files, process arguments, and shell history.

## Requirements

- Python 3.10 or newer.
- OpenSSH clients `ssh`, `scp`, and `ssh-keygen` available on `PATH`.
- `rsync` installed locally and remotely when using `rctl rsync`.
- A working Python `keyring` backend. On macOS, `keyring` uses Keychain.
- Permission to execute every command referenced by a deployment workflow.

## Installation

Install a built wheel:

```sh
python3 -m pip install remctl-0.1.0-py3-none-any.whl
```

Confirm that the command is available:

```sh
rctl --version
```

Installing the wheel does not execute remctl. The first operational command,
such as `rctl list` or `rctl add server.example.com`, automatically creates
`~/.remctl/`, `actions.yaml`, and `workflows.yaml`. Existing configuration files
are never overwritten. `rctl --help`, `rctl --version`, and `rctl uninstall` do
not initialize user configuration.

### Uninstall

Use the remctl-managed uninstall command when you want the option to remove
stored user data as well as the Python package:

```sh
rctl uninstall
```

The command first confirms package removal, then asks whether to also delete:

- All credentials tracked by the Keyring index.
- The Keyring host index.
- `~/.remctl/actions.yaml` and `~/.remctl/workflows.yaml`.

If other files exist in `~/.remctl/`, the directory and those unrelated files
are preserved. Declining the second prompt uninstalls the package but keeps all
configuration and credentials. Direct `pip uninstall remctl` only removes the
Python package and cannot display remctl's user-data prompt.

For development, install the source tree in editable mode:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --editable .
```

## Quick start

Save and validate a credential, connect to the host, and upload a file:

```sh
rctl add server.example.com
rctl ssh server.example.com
rctl scp push ./artifact.jar server.example.com /tmp/artifact.jar
```

The first connection may show an OpenSSH host fingerprint. Verify it through a
trusted channel before accepting it.

Commands that connect to an unregistered host do not require a separate
`rctl add` first. `ssh`, `exec`, `scp`, and `deploy` offer to collect, validate,
and save a missing credential before continuing:

```text
No saved credential for server.example.com. Add it now? [y/N]: y
username: alice
password:
Credential saved for alice@server.example.com
```

If `--user` was supplied, that username is reused and only the password is
requested. Declining the prompt leaves Keyring unchanged and cancels the remote
operation. Hosts with multiple stored accounts still require an explicit
`--user` selection.

## Credential management

### Add credentials

```sh
rctl add server.example.com
```

The command prompts for a username and hidden password:

```text
username: alice
password:
Credential saved for alice@server.example.com
```

Before saving, `rctl add` opens an authentication-only SSH connection. It does
not start a shell or execute a remote command, and a failed login is not stored.
Validation output is quiet by default. Enable OpenSSH diagnostics when needed:

```sh
rctl add server.example.com --debug
```

Credentials use the Keyring service `remctl:<host>`, with the username as the
account and the password as the secret. Running `add` with another username
adds another account without overwriting existing accounts for that host.

### Query credentials

```sh
rctl get server.example.com
rctl get server.example.com alice
```

Without a username, all indexed accounts for the host are shown:

```text
host: server.example.com
username: alice
password: stored
```

Stored passwords are never printed.

### List credentials

```sh
rctl list
rctl ls
```

Example:

```text
HOST                USERNAME
db.example.com      dbadmin
server.example.com  alice
server.example.com  root
```

The host index is stored in Keyring under the `remctl:index` service rather
than in a plaintext project file.

### Repair the credential index

```sh
rctl reindex
```

`reindex` removes entries whose password no longer exists, deletes empty
credentials, and resets an invalid or obsolete index. The cross-platform
`keyring` API cannot enumerate arbitrary entries, so credentials created
outside the index cannot be discovered automatically.

### Delete credentials

Delete one account:

```sh
rctl delete server.example.com alice
```

If the host has one account, the username may be omitted. For a host with
multiple accounts, provide a username or delete every account explicitly:

```sh
rctl delete server.example.com --all
```

`remove`, `rm`, and `del` are aliases for `delete`.

## Remote operations

### Interactive SSH

Use the only stored account:

```sh
rctl ssh 10.0.0.11
```

Select an account when a host has multiple credentials:

```sh
rctl ssh 10.0.0.11 --user root
```

The saved password is sent through the OpenSSH pseudo-terminal. It is not
included in process arguments or printed.

If a stored host key has changed, `rctl` asks before running
`ssh-keygen -R <host>`. After removal, OpenSSH shows the new fingerprint for
confirmation. Do not approve a changed key until it has been verified through a
trusted channel.

### Remote commands

```sh
rctl exec 10.0.0.11 uname -a
rctl exec --user root 10.0.0.11 systemctl status sshd
```

Quote commands containing remote shell operators so the local shell does not
interpret them:

```sh
rctl exec 10.0.0.11 'uptime && df -h'
```

Use `--` if the remote command begins with an option:

```sh
rctl exec 10.0.0.11 -- -example-command
```

Remote output is written directly to the terminal, and `rctl` returns the SSH
process exit code.

### SCP transfers

Upload a file or directory:

```sh
rctl scp push ./artifact.jar 10.0.0.11 /tmp/artifact.jar
rctl scp push ./release 10.0.0.11 /opt/releases/
```

Local directories are detected automatically and transferred recursively.

Download a file or directory:

```sh
rctl scp pull 10.0.0.11 /var/log/hcdadmin.log ./hcdadmin.log
rctl scp pull --recursive 10.0.0.11 /var/log/hcdadmin ./logs
```

Select a stored account with `--user`:

```sh
rctl scp push --user root ./artifact.jar 10.0.0.11 /tmp/
rctl scp pull --user root 10.0.0.11 /tmp/result.txt ./result.txt
```

Both directions report percentage and bandwidth:

```text
[10.0.0.11] pushing  67% 9.2MB/s
[10.0.0.11] completed 100%
```

SCP uses the same credential and host-key verification behavior as SSH.

### Rsync synchronization

Synchronize a local path to a remote host, or pull a remote path locally:

```sh
rctl rsync push ./release/ 10.0.0.11 /opt/release/
rctl rsync pull 10.0.0.11 /var/log/hcdadmin/ ./logs/
```

Archive mode is enabled by default, so directories are copied recursively and
metadata is preserved. Disable it with `--no-archive`. Common synchronization
options are available on both `push` and `pull`:

| Option | Behavior |
| --- | --- |
| `--archive` / `--no-archive` | Enable or disable archive mode. |
| `-z`, `--compress` | Compress data during transfer. |
| `--delete` | Delete destination files that are absent from the source. |
| `--exclude PATTERN` | Exclude a pattern; repeat the option for more patterns. |
| `--dry-run` | Show intended changes without modifying the destination. |
| `--checksum` | Compare file contents by checksum. |
| `--partial` | Retain partial files so interrupted transfers can resume. |
| `--bwlimit KBPS` | Limit bandwidth in KiB per second. |
| `-u`, `--user USER` | Select a saved account. |

For example, preview a compressed mirror while excluding temporary files:

```sh
rctl rsync push --dry-run --compress --delete \
  --exclude '*.tmp' --exclude '.git/' \
  ./release/ 10.0.0.11 /opt/release/
```

`--delete` can remove files from the destination; use `--dry-run` first. Rsync
also gives a trailing slash special meaning: `release/` synchronizes the
directory contents, while `release` synchronizes the directory itself. The
saved password is supplied through the pseudo-terminal and is never added to
the rsync process arguments.

## Deployments and post-workflows

`rctl deploy` validates the local file, selected workflow, and all target
credentials before transferring anything. Targets run concurrently; actions
within one target run sequentially.

Upload a file without running a workflow:

```sh
rctl deploy support-bundle.tar.gz 10.0.0.11 10.0.0.12
```

The generated remote temporary path is retained and printed.

Run a named workflow after each successful upload:

```sh
rctl deploy --post-workflow deploy-archon \
  archon.jar 10.0.0.11 10.0.0.12
```

Use the same named account on all targets:

```sh
rctl deploy --user root --post-workflow deploy-archon \
  archon.jar 10.0.0.11 10.0.0.12
```

### Actions

An action represents one remote operation. These built-in actions are always
available and cannot be overridden:

| Action | Parameters | Behavior |
| --- | --- | --- |
| `copy` | `destination` | Copy the uploaded file to a remote path. |
| `restart-service` | `service` | Restart a systemd service. |
| `docker-image-load` | None | Load the uploaded Docker image archive. |
| `restart-container` | `container` | Restart a Docker container. |

On first use, remctl creates `~/.remctl/actions.yaml` with protected references
to all built-in actions:

```yaml
version: 1
actions:
  copy:
    builtin: true
  restart-service:
    builtin: true
  docker-image-load:
    builtin: true
  restart-container:
    builtin: true
```

Add custom actions to the same mapping when needed:

```yaml
version: 1
actions:
  extract:
    description: Extract an uploaded archive
    command: "tar -xf {remote_path} -C {destination}"
```

Custom commands run through the remote shell. Templates may reference these
runtime values:

- `{remote_path}`: generated remote path containing the uploaded file.
- `{host}`: current target host.
- `{username}`: selected remote username.
- `{local_name}`: local file name.
- Values declared in the workflow step's `with` mapping.

All substituted values are shell-quoted. The action file contains executable
commands and must not be writable by untrusted users.

### Workflows

On first use, remctl creates `~/.remctl/workflows.yaml` with a ready-to-use
Docker image workflow:

```yaml
version: 1
workflows:
  load-image:
    cleanup: always
    steps:
      - action: docker-image-load
```

Run it after uploading an image archive:

```sh
rctl deploy --post-workflow load-image image.tar server.example.com
```

Add further workflows to the same `workflows` mapping. For example:

```yaml
version: 1
workflows:
  deploy-archon:
    cleanup: always
    steps:
      - action: copy
        with:
          destination: /usr/share/hcdserver/hcdadmin/archon.jar
      - action: restart-service
        with:
          service: hcdadmin
```

Every workflow requires at least one step and one cleanup policy:

| Policy | Behavior |
| --- | --- |
| `always` | Attempt cleanup after success, action failure, or transfer failure. |
| `success` | Clean up only after every action succeeds. |
| `never` | Always retain the uploaded temporary file. |

Actions run in order and stop at the first failure. A cleanup failure marks the
target as failed. Destinations, service names, container names, and other action
parameters are defined in YAML and cannot be overridden from the deploy command.

## Development

Create and activate a virtual environment:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --editable .
```

Run the complete test suite:

```sh
python -m unittest discover -s tests
```

Run the package directly:

```sh
rctl --version
python -m remctl --version
```

## Building installation packages

The project uses the PEP 517 build configuration in `pyproject.toml`. The
`build` frontend creates both a wheel and a source distribution in an isolated
environment.

Install the build frontend:

```sh
source .venv/bin/activate
python -m pip install build
```

Run the tests, then build both package formats:

```sh
python -m unittest discover -s tests
python -m build
```

For version `0.1.0`, the generated files are:

```text
dist/remctl-0.1.0-py3-none-any.whl
dist/remctl-0.1.0.tar.gz
```

The wheel is the preferred installation artifact. The source archive contains
the source tree, project metadata, README, and tests. The isolated build may
download the build-system requirement declared in `pyproject.toml`.

Inspect the package contents:

```sh
python -m zipfile --list dist/remctl-0.1.0-py3-none-any.whl
tar -tzf dist/remctl-0.1.0.tar.gz
```

Calculate checksums:

```sh
shasum -a 256 dist/remctl-0.1.0-py3-none-any.whl
shasum -a 256 dist/remctl-0.1.0.tar.gz
```

Test the wheel in a clean virtual environment before distributing it:

```sh
python3 -m venv /tmp/remctl-package-test
/tmp/remctl-package-test/bin/python -m pip install \
  dist/remctl-0.1.0-py3-none-any.whl
/tmp/remctl-package-test/bin/rctl --version
```
