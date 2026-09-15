"""Concurrent deployment workflows for remote hosts."""

import io
import re
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TextIO

from remctl.ssh import run_ssh
from remctl.transfer import run_scp

ARCHON_PATH = (
    "/usr/share/hcdserver/hcdadmin/"
    "archon-1.0-SNAPSHOT-jar-with-dependencies.jar"
)
HCDMGMT_PATH = "/usr/share/hcdserver/hcdmgmt/hcdmgmt-1.0-SNAPSHOT.jar"


class DeployOperation(str, Enum):
    IMAGE = "image"
    ARCHON = "archon"
    HCDMGMT = "hcdmgmt"
    FILE = "file"


@dataclass(frozen=True)
class DeployTarget:
    host: str
    username: str
    password: str


@dataclass(frozen=True)
class DeployResult:
    host: str
    success: bool
    summary: str
    output: str = ""


class ProgressDisplay:
    """Render thread-safe per-host deployment status."""

    def __init__(self, hosts: list[str], stream: TextIO | None = None) -> None:
        self.hosts = hosts
        self.stream = stream if stream is not None else sys.stdout
        self.states = {host: "waiting" for host in hosts}
        self.lock = threading.Lock()
        self.is_terminal = self.stream.isatty()
        if self.is_terminal:
            self.stream.write("\n" * len(hosts))
            self.stream.flush()

    def update(
        self,
        host: str,
        phase: str,
        percent: int | None = None,
        rate: str | None = None,
    ) -> None:
        details = phase
        if percent is not None:
            details += f" {percent:3d}%"
        if rate:
            details += f" {rate}"

        with self.lock:
            self.states[host] = details
            if self.is_terminal:
                self.stream.write(f"\x1b[{len(self.hosts)}A")
                for item_host in self.hosts:
                    self.stream.write(
                        f"\r\x1b[2K[{item_host}] {self.states[item_host]}\n"
                    )
            else:
                self.stream.write(f"[{host}] {details}\n")
            self.stream.flush()


def _temporary_path(local_path: Path) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", local_path.name)
    return f"/tmp/remctl-{secrets.token_hex(6)}-{safe_name}"


def _run_remote_step(
    target: DeployTarget,
    label: str,
    command: tuple[str, ...],
    display: ProgressDisplay,
) -> tuple[int, str]:
    display.update(target.host, label)
    output = io.StringIO()
    exit_code = run_ssh(
        target.host,
        target.username,
        target.password,
        remote_command=command,
        interactive=False,
        output=output,
    )
    return exit_code, output.getvalue()


def _cleanup(
    target: DeployTarget,
    remote_path: str,
    display: ProgressDisplay,
) -> tuple[int, str]:
    return _run_remote_step(
        target,
        "cleaning temporary file",
        ("rm", "-f", "--", remote_path),
        display,
    )


def deploy_to_target(
    local_path: Path,
    target: DeployTarget,
    operation: DeployOperation,
    display: ProgressDisplay,
) -> DeployResult:
    """Transfer and deploy a file to one target."""
    remote_path = _temporary_path(local_path)
    display.update(target.host, "starting transfer", 0, "0B/s")
    exit_code, transcript = run_scp(
        local_path,
        target.host,
        target.username,
        target.password,
        remote_path,
        lambda percent, rate: display.update(
            target.host,
            "transferring",
            percent,
            rate,
        ),
    )
    if exit_code != 0:
        if operation is not DeployOperation.FILE:
            cleanup_code, cleanup_output = _cleanup(target, remote_path, display)
            transcript += cleanup_output
            if cleanup_code != 0:
                display.update(target.host, "transfer and cleanup failed")
                return DeployResult(
                    target.host,
                    False,
                    "file transfer failed; temporary file cleanup failed",
                    transcript,
                )
        display.update(target.host, "transfer failed")
        return DeployResult(target.host, False, "file transfer failed", transcript)

    display.update(target.host, "transfer complete", 100)
    if operation is DeployOperation.FILE:
        display.update(target.host, "completed")
        return DeployResult(target.host, True, f"uploaded to {remote_path}")

    output_parts: list[str] = []
    if operation is DeployOperation.IMAGE:
        exit_code, output = _run_remote_step(
            target,
            "loading Docker image",
            ("docker", "image", "load", "--input", remote_path),
            display,
        )
        output_parts.append(output)
        cleanup_code, cleanup_output = _cleanup(target, remote_path, display)
        output_parts.append(cleanup_output)
        if exit_code != 0:
            display.update(target.host, "Docker image load failed")
            return DeployResult(
                target.host,
                False,
                "docker image load failed",
                "".join(output_parts),
            )
        if cleanup_code != 0:
            display.update(target.host, "temporary file cleanup failed")
            return DeployResult(
                target.host,
                False,
                "image loaded but temporary file cleanup failed",
                "".join(output_parts),
            )
        image_refs = re.findall(r"Loaded image(?: ID)?:\s*(\S+)", output)
        summary = "docker image loaded"
        if image_refs:
            summary += f": {', '.join(image_refs)}"
    else:
        destination = (
            ARCHON_PATH if operation is DeployOperation.ARCHON else HCDMGMT_PATH
        )
        service = "hcdadmin" if operation is DeployOperation.ARCHON else "hcdmgmt"
        exit_code, output = _run_remote_step(
            target,
            f"copying artifact to {destination}",
            ("cp", "--", remote_path, destination),
            display,
        )
        output_parts.append(output)
        cleanup_code, cleanup_output = _cleanup(target, remote_path, display)
        output_parts.append(cleanup_output)
        if exit_code != 0:
            display.update(target.host, "artifact copy failed")
            return DeployResult(
                target.host,
                False,
                "artifact copy failed",
                "".join(output_parts),
            )
        if cleanup_code != 0:
            display.update(target.host, "temporary file cleanup failed")
            return DeployResult(
                target.host,
                False,
                "artifact copied but temporary file cleanup failed",
                "".join(output_parts),
            )
        exit_code, output = _run_remote_step(
            target,
            f"restarting {service}",
            ("systemctl", "restart", service),
            display,
        )
        output_parts.append(output)
        if exit_code != 0:
            display.update(target.host, f"{service} restart failed")
            return DeployResult(
                target.host,
                False,
                f"systemctl restart {service} failed",
                "".join(output_parts),
            )
        summary = f"deployed and restarted {service}"

    display.update(target.host, "completed")
    return DeployResult(target.host, True, summary, "".join(output_parts))


def deploy_many(
    local_path: Path,
    targets: list[DeployTarget],
    operation: DeployOperation,
    stream: TextIO | None = None,
) -> list[DeployResult]:
    """Deploy to all targets concurrently and retain input ordering."""
    display = ProgressDisplay([target.host for target in targets], stream)
    results: dict[str, DeployResult] = {}
    with ThreadPoolExecutor(max_workers=min(len(targets), 16)) as executor:
        futures = {
            executor.submit(
                deploy_to_target,
                local_path,
                target,
                operation,
                display,
            ): target.host
            for target in targets
        }
        for future in as_completed(futures):
            host = futures[future]
            try:
                results[host] = future.result()
            except Exception as error:
                display.update(host, "unexpected failure")
                results[host] = DeployResult(host, False, str(error))
    return [results[target.host] for target in targets]
