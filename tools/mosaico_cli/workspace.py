"""Load the repository-owned ESP-Mosaico workspace configuration."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any

from .errors import EnvironmentError


CONFIG_NAME = ".mosaico.json"
SUPPORTED_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class WorkspaceConfig:
    """Resolved paths owned by the tool checkout and the consuming workspace."""

    tool_root: Path
    root: Path
    config_path: Path
    projects_dir: Path
    default_project: Path | None
    environment_file: Path
    run_dir: Path
    idf_constraint_manifest: Path
    bsp_path: Path
    esp_iris_path: Path
    build_runner: Path
    devices: tuple[dict[str, Any], ...]

    def resolve(self, value: str) -> Path:
        path = Path(value).expanduser()
        return path.resolve() if path.is_absolute() else (self.root / path).resolve()


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EnvironmentError(f"Workspace configuration field '{name}' must be an object.")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EnvironmentError(
            f"Workspace configuration field '{name}' must be a non-empty string."
        )
    return value


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _workspace_config_path(start: Path, explicit: str | None) -> Path:
    requested = explicit or os.environ.get("MOSAICO_WORKSPACE")
    if requested:
        path = Path(requested).expanduser()
        if not path.is_absolute():
            path = start / path
        path = path.resolve()
        return path / CONFIG_NAME if path.is_dir() else path

    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise EnvironmentError(
        f"No {CONFIG_NAME} was found from {start.resolve()} upward. "
        "Run the command inside an ESP-Mosaico workspace or pass --workspace PATH."
    )


def load_workspace(
    tool_root: Path,
    *,
    start: Path | None = None,
    explicit: str | None = None,
) -> WorkspaceConfig:
    """Load and validate the workspace selected by cwd, env, or CLI override."""

    start = (start or Path.cwd()).resolve()
    config_path = _workspace_config_path(start, explicit)
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise EnvironmentError(f"Workspace configuration does not exist: {config_path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise EnvironmentError(f"Invalid workspace configuration: {config_path}") from error
    if not isinstance(value, dict):
        raise EnvironmentError("Workspace configuration must contain a JSON object.")
    if value.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
        raise EnvironmentError(
            "Unsupported workspace configuration schema; expected schema_version 1."
        )

    workspace = _object(value.get("workspace"), "workspace")
    dependencies = _object(value.get("dependencies"), "dependencies")
    build = _object(value.get("build"), "build")
    devices_value = value.get("devices")
    if not isinstance(devices_value, list) or not devices_value:
        raise EnvironmentError(
            "Workspace configuration field 'devices' must be a non-empty array."
        )
    if not all(isinstance(item, dict) for item in devices_value):
        raise EnvironmentError("Every configured device must be an object.")

    root = config_path.parent.resolve()

    def workspace_path(raw: Any, name: str) -> Path:
        text = _string(raw, name)
        path = Path(text).expanduser()
        return path.resolve() if path.is_absolute() else (root / path).resolve()

    default_project_text = _optional_string(
        workspace.get("default_project"), "workspace.default_project"
    )
    default_project = (
        workspace_path(default_project_text, "workspace.default_project")
        if default_project_text
        else None
    )
    runner_value = _string(build.get("runner", "builtin"), "build.runner")
    build_runner = (
        tool_root.resolve()
        / "skills"
        / "idf-low-noise-build"
        / "scripts"
        / "idf_low_noise_build.py"
        if runner_value == "builtin"
        else workspace_path(runner_value, "build.runner")
    )

    return WorkspaceConfig(
        tool_root=tool_root.resolve(),
        root=root,
        config_path=config_path,
        projects_dir=workspace_path(workspace.get("projects_dir"), "workspace.projects_dir"),
        default_project=default_project,
        environment_file=workspace_path(
            workspace.get("environment_file", "Environment"),
            "workspace.environment_file",
        ),
        run_dir=workspace_path(
            workspace.get("run_dir", ".codex-runs/mosaico"),
            "workspace.run_dir",
        ),
        idf_constraint_manifest=workspace_path(
            workspace.get("idf_constraint_manifest"),
            "workspace.idf_constraint_manifest",
        ),
        bsp_path=workspace_path(dependencies.get("bsp"), "dependencies.bsp"),
        esp_iris_path=workspace_path(
            dependencies.get("esp_iris"), "dependencies.esp_iris"
        ),
        build_runner=build_runner,
        devices=tuple(dict(item) for item in devices_value),
    )
