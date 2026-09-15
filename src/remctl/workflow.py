"""Action and workflow definitions for post-transfer deployment behavior."""

import os
import re
import shlex
import string
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path.home() / ".remctl"
ACTIONS_PATH = CONFIG_DIR / "actions.yaml"
WORKFLOWS_PATH = CONFIG_DIR / "workflows.yaml"
DEFAULT_ACTIONS_CONFIG = """\
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
"""
DEFAULT_WORKFLOWS_CONFIG = """\
version: 1
workflows:
  load-image:
    cleanup: always
    steps:
      - action: docker-image-load
"""
RUNTIME_PARAMETERS = frozenset({"remote_path", "host", "username", "local_name"})
_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_PARAMETER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class WorkflowConfigError(ValueError):
    """Raised when an action or workflow configuration is invalid."""


class CleanupPolicy(str, Enum):
    ALWAYS = "always"
    SUCCESS = "success"
    NEVER = "never"


@dataclass(frozen=True)
class ActionDefinition:
    name: str
    command: str
    description: str = ""

    @property
    def parameters(self) -> frozenset[str]:
        return _template_parameters(self.command) - RUNTIME_PARAMETERS


@dataclass(frozen=True)
class WorkflowStep:
    action: ActionDefinition
    values: dict[str, str]


@dataclass(frozen=True)
class WorkflowDefinition:
    name: str
    cleanup: CleanupPolicy
    steps: tuple[WorkflowStep, ...]


BUILTIN_ACTIONS = {
    action.name: action
    for action in (
        ActionDefinition(
            "copy",
            "cp -- {remote_path} {destination}",
            "copy the uploaded file to a destination",
        ),
        ActionDefinition(
            "restart-service",
            "systemctl restart {service}",
            "restart a systemd service",
        ),
        ActionDefinition(
            "docker-image-load",
            "docker image load --input {remote_path}",
            "load an uploaded Docker image archive",
        ),
        ActionDefinition(
            "restart-container",
            "docker restart {container}",
            "restart a Docker container",
        ),
    )
}


def _create_default_file(path: Path, content: str) -> bool:
    """Create one private configuration file without replacing existing data."""
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return True


def initialize_config(config_dir: Path = CONFIG_DIR) -> tuple[Path, ...]:
    """Create the user configuration directory and missing default files."""
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    defaults = (
        (config_dir / "actions.yaml", DEFAULT_ACTIONS_CONFIG),
        (config_dir / "workflows.yaml", DEFAULT_WORKFLOWS_CONFIG),
    )
    return tuple(
        path for path, content in defaults if _create_default_file(path, content)
    )


def _template_parameters(command: str) -> frozenset[str]:
    parameters: set[str] = set()
    try:
        parsed = string.Formatter().parse(command)
        for _, field_name, format_spec, conversion in parsed:
            if field_name is None:
                continue
            if not _PARAMETER_PATTERN.fullmatch(field_name):
                raise WorkflowConfigError(
                    f"invalid action placeholder: {field_name!r}"
                )
            if format_spec or conversion:
                raise WorkflowConfigError(
                    f"action placeholder modifiers are not supported: {field_name}"
                )
            parameters.add(field_name)
    except ValueError as error:
        raise WorkflowConfigError(
            f"invalid action command template: {error}"
        ) from error
    return frozenset(parameters)


def _load_yaml(path: Path, kind: str) -> dict[str, Any]:
    try:
        with path.open(encoding="utf-8") as stream:
            document = yaml.safe_load(stream)
    except OSError as error:
        raise WorkflowConfigError(
            f"unable to read {kind} file {path}: {error}"
        ) from error
    except yaml.YAMLError as error:
        raise WorkflowConfigError(
            f"invalid YAML in {kind} file {path}: {error}"
        ) from error
    if not isinstance(document, dict):
        raise WorkflowConfigError(f"{kind} file must contain a mapping: {path}")
    if type(document.get("version")) is not int or document["version"] != 1:
        raise WorkflowConfigError(f"{kind} file must declare version: 1")
    return document


def _validate_name(name: object, kind: str) -> str:
    if not isinstance(name, str) or not _NAME_PATTERN.fullmatch(name):
        raise WorkflowConfigError(f"invalid {kind} name: {name!r}")
    return name


def _validate_parameter_name(name: object) -> str:
    if not isinstance(name, str) or not _PARAMETER_PATTERN.fullmatch(name):
        raise WorkflowConfigError(f"invalid parameter name: {name!r}")
    return name


def load_actions(path: Path = ACTIONS_PATH) -> dict[str, ActionDefinition]:
    """Load built-in actions and optional user-defined actions."""
    actions = dict(BUILTIN_ACTIONS)
    if not path.exists():
        return actions

    document = _load_yaml(path, "action configuration")
    unknown_root = set(document) - {"version", "actions"}
    if unknown_root:
        raise WorkflowConfigError(
            "action configuration has unknown fields: "
            + ", ".join(sorted(unknown_root))
        )
    raw_actions = document.get("actions")
    if not isinstance(raw_actions, dict):
        raise WorkflowConfigError(
            "action configuration must contain an actions mapping"
        )

    for raw_name, raw_definition in raw_actions.items():
        name = _validate_name(raw_name, "action")
        if name in BUILTIN_ACTIONS:
            if raw_definition == {"builtin": True}:
                continue
            raise WorkflowConfigError(f"custom action cannot override built-in: {name}")
        if not isinstance(raw_definition, dict):
            raise WorkflowConfigError(f"action {name} must be a mapping")
        unknown = set(raw_definition) - {"command", "description"}
        if unknown:
            raise WorkflowConfigError(
                f"action {name} has unknown fields: {', '.join(sorted(unknown))}"
            )
        command = raw_definition.get("command")
        description = raw_definition.get("description", "")
        if not isinstance(command, str) or not command.strip():
            raise WorkflowConfigError(f"action {name} must define a command string")
        if not isinstance(description, str):
            raise WorkflowConfigError(f"action {name} description must be a string")
        action = ActionDefinition(name, command, description)
        _template_parameters(command)
        actions[name] = action
    return actions


def _scalar_string(value: object, label: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise WorkflowConfigError(f"{label} must be a scalar value")


def load_workflow(
    name: str,
    actions: dict[str, ActionDefinition],
    path: Path = WORKFLOWS_PATH,
) -> WorkflowDefinition:
    """Load and validate one named workflow."""
    _validate_name(name, "workflow")
    document = _load_yaml(path, "workflow configuration")
    unknown_root = set(document) - {"version", "workflows"}
    if unknown_root:
        raise WorkflowConfigError(
            "workflow configuration has unknown fields: "
            + ", ".join(sorted(unknown_root))
        )
    raw_workflows = document.get("workflows")
    if not isinstance(raw_workflows, dict):
        raise WorkflowConfigError(
            "workflow configuration must contain a workflows mapping"
        )
    if name not in raw_workflows:
        raise WorkflowConfigError(f"workflow not found: {name}")

    raw_workflow = raw_workflows[name]
    if not isinstance(raw_workflow, dict):
        raise WorkflowConfigError(f"workflow {name} must be a mapping")
    unknown = set(raw_workflow) - {"cleanup", "steps"}
    if unknown:
        raise WorkflowConfigError(
            f"workflow {name} has unknown fields: {', '.join(sorted(unknown))}"
        )
    try:
        cleanup = CleanupPolicy(raw_workflow.get("cleanup"))
    except ValueError as error:
        raise WorkflowConfigError(
            f"workflow {name} cleanup must be always, success, or never"
        ) from error

    raw_steps = raw_workflow.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise WorkflowConfigError(f"workflow {name} must contain at least one step")

    steps: list[WorkflowStep] = []
    for position, raw_step in enumerate(raw_steps, start=1):
        label = f"workflow {name} step {position}"
        if not isinstance(raw_step, dict):
            raise WorkflowConfigError(f"{label} must be a mapping")
        unknown = set(raw_step) - {"action", "with"}
        if unknown:
            raise WorkflowConfigError(
                f"{label} has unknown fields: {', '.join(sorted(unknown))}"
            )
        action_name = raw_step.get("action")
        if not isinstance(action_name, str) or action_name not in actions:
            raise WorkflowConfigError(
                f"{label} references unknown action: {action_name}"
            )
        raw_values = raw_step.get("with", {})
        if not isinstance(raw_values, dict):
            raise WorkflowConfigError(f"{label} with must be a mapping")
        if RUNTIME_PARAMETERS.intersection(raw_values):
            reserved = sorted(RUNTIME_PARAMETERS.intersection(raw_values))
            raise WorkflowConfigError(
                f"{label} cannot override runtime parameters: {', '.join(reserved)}"
            )
        values = {
            _validate_parameter_name(key): _scalar_string(value, f"{label} {key}")
            for key, value in raw_values.items()
        }
        action = actions[action_name]
        missing = action.parameters - values.keys()
        extra = values.keys() - action.parameters
        if missing:
            raise WorkflowConfigError(
                f"{label} is missing parameters: {', '.join(sorted(missing))}"
            )
        if extra:
            raise WorkflowConfigError(
                f"{label} has unused parameters: {', '.join(sorted(extra))}"
            )
        steps.append(WorkflowStep(action, values))
    return WorkflowDefinition(name, cleanup, tuple(steps))


def render_action(step: WorkflowStep, context: dict[str, str]) -> str:
    """Render an action command with shell-quoted workflow and runtime values."""
    missing_context = RUNTIME_PARAMETERS - context.keys()
    if missing_context:
        raise WorkflowConfigError(
            f"missing runtime parameters: {', '.join(sorted(missing_context))}"
        )
    values = {**context, **step.values}
    quoted = {key: shlex.quote(value) for key, value in values.items()}
    return step.action.command.format_map(quoted)
