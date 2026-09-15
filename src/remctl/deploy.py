"""Concurrent deployment workflows for remote hosts."""

import io
import re
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from remctl.ssh import run_ssh
from remctl.transfer import run_scp
from remctl.workflow import CleanupPolicy, WorkflowDefinition, render_action


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
    command: str,
    display: ProgressDisplay,
) -> tuple[int, str]:
    display.update(target.host, label)
    output = io.StringIO()
    exit_code = run_ssh(
        target.host,
        target.username,
        target.password,
        remote_command=(command,),
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
        f"rm -f -- {remote_path}",
        display,
    )


def _cleanup_after_failure(
    target: DeployTarget,
    remote_path: str,
    display: ProgressDisplay,
    summary: str,
    output: str,
) -> DeployResult:
    cleanup_code, cleanup_output = _cleanup(target, remote_path, display)
    combined_output = output + cleanup_output
    if cleanup_code != 0:
        display.update(target.host, "operation and cleanup failed")
        return DeployResult(
            target.host,
            False,
            f"{summary}; temporary file cleanup failed",
            combined_output,
        )
    display.update(target.host, "failed")
    return DeployResult(target.host, False, summary, combined_output)


def deploy_to_target(
    local_path: Path,
    target: DeployTarget,
    workflow: WorkflowDefinition | None,
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
        if workflow is not None and workflow.cleanup is CleanupPolicy.ALWAYS:
            return _cleanup_after_failure(
                target,
                remote_path,
                display,
                "file transfer failed",
                transcript,
            )
        display.update(target.host, "transfer failed")
        return DeployResult(target.host, False, "file transfer failed", transcript)

    display.update(target.host, "transfer complete", 100)
    if workflow is None:
        display.update(target.host, "completed")
        return DeployResult(target.host, True, f"uploaded to {remote_path}")

    context = {
        "remote_path": remote_path,
        "host": target.host,
        "username": target.username,
        "local_name": local_path.name,
    }
    output_parts: list[str] = []
    for step in workflow.steps:
        command = render_action(step, context)
        exit_code, output = _run_remote_step(
            target,
            f"running {step.action.name}",
            command,
            display,
        )
        output_parts.append(output)
        if exit_code != 0:
            summary = f"workflow {workflow.name} failed at {step.action.name}"
            if workflow.cleanup is CleanupPolicy.ALWAYS:
                return _cleanup_after_failure(
                    target,
                    remote_path,
                    display,
                    summary,
                    "".join(output_parts),
                )
            display.update(target.host, f"{step.action.name} failed")
            return DeployResult(
                target.host,
                False,
                summary,
                "".join(output_parts),
            )

    if workflow.cleanup in {CleanupPolicy.ALWAYS, CleanupPolicy.SUCCESS}:
        cleanup_code, cleanup_output = _cleanup(target, remote_path, display)
        output_parts.append(cleanup_output)
        if cleanup_code != 0:
            display.update(target.host, "temporary file cleanup failed")
            return DeployResult(
                target.host,
                False,
                f"workflow {workflow.name} completed but cleanup failed",
                "".join(output_parts),
            )

    display.update(target.host, "completed")
    summary = f"workflow {workflow.name} completed"
    if workflow.cleanup is CleanupPolicy.NEVER:
        summary += f"; uploaded file retained at {remote_path}"
    return DeployResult(target.host, True, summary, "".join(output_parts))


def deploy_many(
    local_path: Path,
    targets: list[DeployTarget],
    workflow: WorkflowDefinition | None,
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
                workflow,
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
