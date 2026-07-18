"""YAML file loading utilities.

Only :func:`yaml.safe_load` is used; arbitrary Python object construction
through PyYAML tags is never permitted.
"""

from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Union

import yaml

from .exceptions import YamlConfigurationError

#: Groups granted full RWCU on every fake model when ``acl`` is left on.
_DEFAULT_FAKE_MODEL_GROUPS: List[str] = ["base.group_user"]

#: Keys accepted inside a ``fake_models:`` mapping.
#:
#: Unlike the top-level document — which is deliberately pull-based and ignores
#: unknown keys so old files keep loading — this block *is* whitelisted. It is
#: brand new, so nothing can break by being strict, and a silently ignored
#: ``acls: false`` typo would hand the scenario an ACL it explicitly declined.
_FAKE_MODEL_KEYS: FrozenSet[str] = frozenset({"classes", "acl", "groups", "addon"})


def load_yaml_file(path: Union[str, Path]) -> Dict[str, Any]:
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


def validate_scenarios_document(data: Dict[str, Any], source: str) -> List[Dict[str, Any]]:
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
        options = scenario.get("options")
        if options is not None and not isinstance(options, dict):
            raise YamlConfigurationError(
                f"'options' in scenario {scenario.get('name')!r} of {source} must be a mapping"
            )
    return scenarios


def extract_setup_steps(data: Dict[str, Any], source: str) -> List[Dict[str, Any]]:
    """Return the optional top-level ``setup:`` steps.

    Setup steps are replayed before every scenario, against a freshly reset
    registry — so scenarios can share fixtures without sharing state.

    Args:
        data: Parsed YAML mapping.
        source: A label identifying the source (file path) for errors.

    Returns:
        The setup step list, empty when the document declares no ``setup``.

    Raises:
        YamlConfigurationError: when ``setup`` is present but malformed.
    """
    setup = data.get("setup")
    if setup is None:
        return []
    if not isinstance(setup, dict):
        raise YamlConfigurationError(
            f"'setup' in {source} must be a mapping with a 'steps' list, got {type(setup).__name__}"
        )
    steps = setup.get("steps")
    if not isinstance(steps, list):
        raise YamlConfigurationError(f"'setup.steps' in {source} must be a list")
    return steps


def extract_options(data: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Return the optional top-level ``options:`` mapping.

    Args:
        data: Parsed YAML mapping.
        source: A label identifying the source (file path) for errors.

    Returns:
        The options mapping, empty when the document declares none.

    Raises:
        YamlConfigurationError: when ``options`` is present but not a mapping.
    """
    options = data.get("options")
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise YamlConfigurationError(
            f"'options' in {source} must be a mapping, got {type(options).__name__}"
        )
    return options


def _validate_class_references(classes: Any, source: str) -> List[str]:
    """Validate ``fake_models.classes`` and return it as a list of strings."""
    if not isinstance(classes, list) or not classes:
        raise YamlConfigurationError(
            f"'fake_models.classes' in {source} must be a non-empty list of "
            f"'module.path:ClassName' strings"
        )
    for index, ref in enumerate(classes):
        if not isinstance(ref, str):
            raise YamlConfigurationError(
                f"'fake_models.classes[{index}]' in {source} must be a string, "
                f"got {type(ref).__name__}"
            )
        module_name, separator, class_name = ref.partition(":")
        if not separator or not module_name.strip() or not class_name.strip():
            raise YamlConfigurationError(
                f"'fake_models.classes[{index}]' in {source} must have the form "
                f"'module.path:ClassName' — a single ':' separating an importable "
                f"module from a class name — got {ref!r}"
            )
    return list(classes)


def _validate_group_references(groups: Any, source: str) -> List[str]:
    """Validate ``fake_models.groups`` and return it as a list of strings."""
    if not isinstance(groups, list) or not groups:
        raise YamlConfigurationError(
            f"'fake_models.groups' in {source} must be a non-empty list of xml_id strings"
        )
    for index, group in enumerate(groups):
        if not isinstance(group, str):
            raise YamlConfigurationError(
                f"'fake_models.groups[{index}]' in {source} must be a string, "
                f"got {type(group).__name__}"
            )
    return list(groups)


def extract_fake_models(data: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Return the normalised top-level ``fake_models:`` block.

    Accepts a short form (a plain list of ``"module.path:ClassName"`` strings)
    or a long form (a mapping with ``classes`` plus options). Both normalise to
    the same shape, so callers never branch on which was written.

    Class references are *strings*, not Python imports, on purpose: the classes
    must not be imported until the registry has been snapshotted, and a string
    is the only way a YAML file can name one without importing it.

    Args:
        data: Parsed YAML mapping.
        source: A label identifying the source (file path) for errors.

    Returns:
        A mapping with keys ``classes`` (list of str), ``acl`` (bool),
        ``groups`` (list of str) and ``addon`` (str or None). Empty dict when
        the document declares no ``fake_models``.

    Raises:
        YamlConfigurationError: when ``fake_models`` is present but malformed.
    """
    raw = data.get("fake_models")
    if raw is None:
        return {}

    if isinstance(raw, list):
        block: Dict[str, Any] = {"classes": raw}
    elif isinstance(raw, dict):
        block = dict(raw)
    else:
        raise YamlConfigurationError(
            f"'fake_models' in {source} must be a list of class references or a "
            f"mapping with a 'classes' list, got {type(raw).__name__}"
        )

    unknown = sorted(set(block) - _FAKE_MODEL_KEYS)
    if unknown:
        raise YamlConfigurationError(
            f"Unknown key(s) {unknown} in 'fake_models' of {source}. "
            f"Valid keys: {sorted(_FAKE_MODEL_KEYS)}"
        )

    classes = _validate_class_references(block.get("classes"), source)

    acl = block.get("acl", True)
    if not isinstance(acl, bool):
        raise YamlConfigurationError(
            f"'fake_models.acl' in {source} must be a boolean, got {type(acl).__name__}"
        )

    groups = _validate_group_references(block.get("groups", _DEFAULT_FAKE_MODEL_GROUPS), source)

    addon = block.get("addon")
    if addon is not None and not isinstance(addon, str):
        raise YamlConfigurationError(
            f"'fake_models.addon' in {source} must be a string, got {type(addon).__name__}"
        )

    return {
        "classes": classes,
        "acl": acl,
        "groups": groups,
        "addon": addon,
    }
