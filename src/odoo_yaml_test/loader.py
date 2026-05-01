"""YAML file loading utilities.

Only :func:`yaml.safe_load` is used; arbitrary Python object construction
through PyYAML tags is never permitted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .exceptions import YamlConfigurationError


def load_yaml_file(path: str | Path) -> dict[str, Any]:
    """Load a YAML file and return the parsed mapping.

    Args:
        path: Filesystem path to a YAML file.

    Returns:
        The parsed top-level mapping.

    Raises:
        YamlConfigurationError: when the file does not exist, is not
            readable, is not valid YAML, or its top-level node is not a
            mapping.

    Example:
        >>> data = load_yaml_file("scenarios.yaml")  # doctest: +SKIP
        >>> "scenarios" in data  # doctest: +SKIP
        True
    """
    file_path = Path(path)
    if not file_path.is_file():
        raise YamlConfigurationError(f"YAML file not found: {file_path}")

    try:
        with file_path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise YamlConfigurationError(f"Failed to parse YAML file {file_path}: {exc}") from exc

    if data is None:
        raise YamlConfigurationError(f"YAML file is empty: {file_path}")
    if not isinstance(data, dict):
        raise YamlConfigurationError(
            f"YAML file {file_path} must contain a top-level mapping, got {type(data).__name__}"
        )
    return data


def validate_scenarios_document(data: dict[str, Any], source: str) -> list[dict[str, Any]]:
    """Validate the top-level document and return its scenario list.

    Args:
        data: Parsed YAML mapping.
        source: A label identifying the source (file path) for errors.

    Returns:
        The list of scenario mappings.

    Raises:
        YamlConfigurationError: when the document does not match the
            expected ``scenarios:`` shape.
    """
    if "scenarios" not in data:
        raise YamlConfigurationError(f"YAML document {source} is missing top-level key 'scenarios'")
    scenarios = data["scenarios"]
    if not isinstance(scenarios, list):
        raise YamlConfigurationError(
            f"'scenarios' in {source} must be a list, got {type(scenarios).__name__}"
        )
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise YamlConfigurationError(f"Scenario at index {index} in {source} must be a mapping")
        if "name" not in scenario:
            raise YamlConfigurationError(f"Scenario at index {index} in {source} is missing 'name'")
        if "steps" not in scenario or not isinstance(scenario["steps"], list):
            raise YamlConfigurationError(
                f"Scenario {scenario.get('name')!r} in {source} must have a 'steps' list"
            )
    return scenarios
