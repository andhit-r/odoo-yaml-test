"""Main :class:`YamlTransactionCase` implementation.

This module imports Odoo lazily so the rest of the package remains
importable in environments where Odoo is not installed (CI linting,
unit-testing the evaluator, etc.).
"""

import inspect
import logging
import re
from pathlib import Path
from typing import Any, Dict, FrozenSet, Optional

from .evaluator import safe_eval
from .exceptions import YamlAssertionError, YamlConfigurationError, YamlStepError
from .loader import load_yaml_file, validate_scenarios_document

_LOGGER = logging.getLogger("odoo_yaml_test")

#: Regex matching ``module.xml_id`` style strings. Conservative: requires a
#: lowercase module prefix to limit false positives.
_XML_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[A-Za-z0-9_.]+$")

#: Relational ORM field types.
_RELATIONAL_TYPES: FrozenSet[str] = frozenset({"many2one", "one2many", "many2many", "reference"})

_VALID_OPERATORS: FrozenSet[str] = frozenset(
    {
        "equals",
        "not_equals",
        "gt",
        "gte",
        "lt",
        "lte",
        "in",
        "not_in",
        "contains",
        "is_truthy",
        "is_falsy",
    }
)


try:
    from odoo.tests.common import TransactionCase as _TransactionCase
except ImportError:  # pragma: no cover - import guard
    # Provide a placeholder so the module remains importable in non-Odoo
    # environments (linting, isolated unit tests). Instantiation is
    # blocked in __init_subclass__ below.
    class _TransactionCase:  # type: ignore[no-redef]
        """Placeholder used when Odoo is not available."""

        _odoo_yaml_test_placeholder = True

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise ImportError(
                "odoo-yaml-test requires Odoo to be importable when "
                "YamlTransactionCase is instantiated. Install/activate "
                "Odoo first, or use the package's standalone helpers "
                "(evaluator, loader) without subclassing "
                "YamlTransactionCase."
            )


class YamlTransactionCase(_TransactionCase):  # type: ignore[misc]
    """A :class:`TransactionCase` that runs scenarios declared in YAML.

    Subclasses typically only need to call :meth:`run_yaml_scenario` from
    a test method; the YAML file is auto-discovered relative to the
    subclass's source file.

    Example:
        >>> from odoo_yaml_test import YamlTransactionCase
        >>> class TestSale(YamlTransactionCase):  # doctest: +SKIP
        ...     def test_b2b(self):
        ...         self.run_yaml_scenario("test_data.yaml")
    """

    #: Per-scenario record registry. Reset at the start of each scenario.
    registry: Dict[str, Any]

    def setUp(self) -> None:
        super().setUp()
        self.registry = {}

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def run_yaml_scenario(self, filename: str) -> None:
        """Execute every scenario declared in *filename*.

        The file is resolved relative to the directory of the test
        class's source file via :func:`inspect.getfile`. Each scenario
        runs inside its own ``subTest`` so a failure in one does not
        block the others.

        Args:
            filename: Name (or relative path) of the YAML file.
        """
        yaml_path = self._resolve_yaml_path(filename)
        document = load_yaml_file(yaml_path)
        scenarios = validate_scenarios_document(document, str(yaml_path))

        for scenario in scenarios:
            scenario_name = scenario["name"]
            with self.subTest(scenario=scenario_name, file=str(yaml_path)):
                self.registry = {}
                self._run_scenario(scenario, str(yaml_path))

    def run_yaml_scenarios(self, *filenames: str) -> None:
        """Execute multiple YAML files in order.

        Args:
            *filenames: One or more YAML filenames.
        """
        for filename in filenames:
            self.run_yaml_scenario(filename)

    # ------------------------------------------------------------------
    # Scenario / step execution
    # ------------------------------------------------------------------

    def _resolve_yaml_path(self, filename: str) -> Path:
        """Resolve *filename* relative to the test subclass's source file."""
        candidate = Path(filename)
        if candidate.is_absolute() and candidate.is_file():
            return candidate

        try:
            test_file = Path(inspect.getfile(self.__class__))
        except TypeError as exc:  # pragma: no cover - dynamic class edge case
            raise YamlConfigurationError(
                f"Cannot locate source file for {self.__class__!r}: {exc}"
            ) from exc

        resolved = (test_file.parent / filename).resolve()
        if not resolved.is_file():
            raise YamlConfigurationError(f"YAML file {filename!r} not found at {resolved}")
        return resolved

    def _run_scenario(self, scenario: Dict[str, Any], yaml_file: str) -> None:
        """Execute every step in *scenario*."""
        scenario_name = scenario["name"]
        for index, step in enumerate(scenario["steps"]):
            step_name = step.get("step", f"<step {index}>")
            action = step.get("action")
            _LOGGER.info("[%s::%s::%s] action=%s", yaml_file, scenario_name, step_name, action)
            try:
                self._dispatch_step(step)
            except YamlAssertionError:
                # Re-wrap to add context but keep AssertionError lineage.
                raise
            except Exception as exc:
                _LOGGER.error(
                    "[%s::%s::%s] failed: %s",
                    yaml_file,
                    scenario_name,
                    step_name,
                    exc,
                )
                raise YamlStepError(
                    f"Error in File: {yaml_file} -> Scenario: {scenario_name!r} "
                    f"-> Step: {step_name!r} (action={action}): "
                    f"{type(exc).__name__}: {exc}"
                ) from exc

    def _dispatch_step(self, step: Dict[str, Any]) -> None:
        """Dispatch a single step to the matching action handler."""
        action = step.get("action")
        if not action:
            raise YamlConfigurationError(f"Step is missing 'action': {step!r}")

        handler_name = f"_action_{action}"
        handler = getattr(self, handler_name, None)
        if handler is None:
            raise YamlConfigurationError(f"Unknown action: {action!r}")
        handler(step)

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    def _action_create(self, step: Dict[str, Any]) -> None:
        """Handle ``action: create``."""
        model_name = self._require(step, "model")
        values = self._require(step, "values", expected_type=dict)
        env = self._build_env(step)
        model = env[model_name]
        resolved_values = self._resolve_values(values, model)
        record = model.create(resolved_values)
        save_as = step.get("save_as")
        if save_as:
            self.registry[save_as] = record

    def _action_write(self, step: Dict[str, Any]) -> None:
        """Handle ``action: write``."""
        target = self._require(step, "target")
        values = self._require(step, "values", expected_type=dict)
        record = self._resolve_target(target)
        env = self._build_env(step, record=record)
        record_in_env = record.with_env(env) if env is not record.env else record
        resolved_values = self._resolve_values(values, record_in_env)
        record_in_env.write(resolved_values)

    def _action_call(self, step: Dict[str, Any]) -> None:
        """Handle ``action: call``."""
        target = self._require(step, "target")
        method_name = self._require(step, "method")
        record = self._resolve_target(target)
        env = self._build_env(step, record=record)
        record_in_env = record.with_env(env) if env is not record.env else record

        args = self._resolve_value_recursive(step.get("args", []), field_type=None)
        kwargs = self._resolve_value_recursive(step.get("kwargs", {}), field_type=None)
        if not isinstance(args, list):
            raise YamlConfigurationError(f"'args' must be a list, got {type(args).__name__}")
        if not isinstance(kwargs, dict):
            raise YamlConfigurationError(f"'kwargs' must be a mapping, got {type(kwargs).__name__}")

        method = getattr(record_in_env, method_name)
        method(*args, **kwargs)

        asserts = step.get("asserts")
        if asserts:
            self._run_asserts(record_in_env, asserts)

    def _action_assert(self, step: Dict[str, Any]) -> None:
        """Handle ``action: assert``."""
        target = self._require(step, "target")
        asserts = self._require(step, "asserts", expected_type=dict)
        record = self._resolve_target(target)
        self._run_asserts(record, asserts)

    def _action_ref(self, step: Dict[str, Any]) -> None:
        """Handle ``action: ref``."""
        xml_id = self._require(step, "xml_id")
        save_as = self._require(step, "save_as")
        self.registry[save_as] = self.env.ref(xml_id)

    def _action_search(self, step: Dict[str, Any]) -> None:
        """Handle ``action: search``."""
        model_name = self._require(step, "model")
        domain = self._require(step, "domain", expected_type=list)
        save_as = self._require(step, "save_as")
        env = self._build_env(step)
        model = env[model_name]

        resolved_domain = self._resolve_value_recursive(domain, field_type=None)
        kwargs: dict[str, Any] = {}
        if "limit" in step:
            kwargs["limit"] = step["limit"]
        if "order" in step:
            kwargs["order"] = step["order"]
        records = model.search(resolved_domain, **kwargs)

        if "expect_count" in step and len(records) != step["expect_count"]:
            raise YamlAssertionError(
                f"search expected {step['expect_count']} records, got {len(records)}"
            )
        self.registry[save_as] = records

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------

    def _run_asserts(self, record: Any, asserts: Dict[str, Any]) -> None:
        """Run a mapping of ``field_name -> spec`` assertions on *record*."""
        if not isinstance(asserts, dict):
            raise YamlConfigurationError(
                f"'asserts' must be a mapping, got {type(asserts).__name__}"
            )
        for field_name, spec in asserts.items():
            if not isinstance(spec, dict):
                raise YamlConfigurationError(f"Assert spec for {field_name!r} must be a mapping")
            assert_type = spec.get("type", "value")
            method = getattr(self, f"_assert_{assert_type}", None)
            if method is None:
                raise YamlConfigurationError(f"Unknown assert type: {assert_type!r}")
            method(record, field_name, spec)

    def _assert_value(self, record: Any, field_name: str, spec: Dict[str, Any]) -> None:
        """``type: value`` — comparison-operator assertion."""
        operator = spec.get("operator", "equals")
        if operator not in _VALID_OPERATORS:
            raise YamlConfigurationError(
                f"Unknown operator {operator!r}. Valid: {sorted(_VALID_OPERATORS)}"
            )
        actual = getattr(record, field_name)
        expected = spec.get("expected")

        if operator == "equals" and actual != expected:
            raise YamlAssertionError(
                f"{record._name}.{field_name}: expected {expected!r}, got {actual!r}"
            )
        if operator == "not_equals" and actual == expected:
            raise YamlAssertionError(
                f"{record._name}.{field_name}: expected != {expected!r}, but equal"
            )
        if operator == "gt" and not actual > expected:
            raise YamlAssertionError(
                f"{record._name}.{field_name}: expected > {expected!r}, got {actual!r}"
            )
        if operator == "gte" and not actual >= expected:
            raise YamlAssertionError(
                f"{record._name}.{field_name}: expected >= {expected!r}, got {actual!r}"
            )
        if operator == "lt" and not actual < expected:
            raise YamlAssertionError(
                f"{record._name}.{field_name}: expected < {expected!r}, got {actual!r}"
            )
        if operator == "lte" and not actual <= expected:
            raise YamlAssertionError(
                f"{record._name}.{field_name}: expected <= {expected!r}, got {actual!r}"
            )
        if operator == "in":
            if expected is None:
                raise YamlConfigurationError(
                    f"'in' operator on {field_name!r} requires 'expected' to be a container"
                )
            if actual not in expected:
                raise YamlAssertionError(
                    f"{record._name}.{field_name}: {actual!r} not in {expected!r}"
                )
        if operator == "not_in":
            if expected is None:
                raise YamlConfigurationError(
                    f"'not_in' operator on {field_name!r} requires 'expected' to be a container"
                )
            if actual in expected:
                raise YamlAssertionError(
                    f"{record._name}.{field_name}: {actual!r} unexpectedly in {expected!r}"
                )
        if operator == "contains" and expected not in actual:
            raise YamlAssertionError(f"{record._name}.{field_name}: {expected!r} not in {actual!r}")
        if operator == "is_truthy" and not actual:
            raise YamlAssertionError(
                f"{record._name}.{field_name}: expected truthy, got {actual!r}"
            )
        if operator == "is_falsy" and actual:
            raise YamlAssertionError(f"{record._name}.{field_name}: expected falsy, got {actual!r}")

    def _assert_m2o(self, record: Any, field_name: str, spec: Dict[str, Any]) -> None:
        """``type: m2o`` — compares the related id with ``self.env.ref``."""
        expected_xml_id = spec.get("expected_xml_id")
        if not expected_xml_id:
            raise YamlConfigurationError(f"m2o assert on {field_name!r} requires 'expected_xml_id'")
        actual = getattr(record, field_name)
        expected_record = self.env.ref(expected_xml_id)
        if actual.id != expected_record.id:
            raise YamlAssertionError(
                f"{record._name}.{field_name}: expected ref {expected_xml_id!r} "
                f"(id={expected_record.id}), got id={actual.id}"
            )

    def _assert_o2m(self, record: Any, field_name: str, spec: Dict[str, Any]) -> None:
        """``type: o2m`` — count or membership assertion."""
        self._assert_relation(record, field_name, spec, kind="o2m")

    def _assert_m2m(self, record: Any, field_name: str, spec: Dict[str, Any]) -> None:
        """``type: m2m`` — count or membership assertion."""
        self._assert_relation(record, field_name, spec, kind="m2m")

    def _assert_relation(
        self, record: Any, field_name: str, spec: Dict[str, Any], kind: str
    ) -> None:
        check = spec.get("check", "count")
        recordset = getattr(record, field_name)

        if check == "count":
            expected_count = spec.get("expected_count")
            if expected_count is None:
                raise YamlConfigurationError(
                    f"{kind} count check on {field_name!r} requires 'expected_count'"
                )
            if len(recordset) != expected_count:
                raise YamlAssertionError(
                    f"{record._name}.{field_name}: expected {expected_count} "
                    f"records, got {len(recordset)}"
                )
        elif check in ("contains_xml_ids", "exact_xml_ids"):
            expected_xml_ids = spec.get("expected_xml_ids") or []
            expected_ids = {self.env.ref(xid).id for xid in expected_xml_ids}
            actual_ids = set(recordset.ids)
            if check == "contains_xml_ids" and not expected_ids.issubset(actual_ids):
                missing = expected_ids - actual_ids
                raise YamlAssertionError(
                    f"{record._name}.{field_name}: missing ids {missing} from {actual_ids}"
                )
            if check == "exact_xml_ids" and expected_ids != actual_ids:
                raise YamlAssertionError(
                    f"{record._name}.{field_name}: expected exact ids {expected_ids}, "
                    f"got {actual_ids}"
                )
        else:
            raise YamlConfigurationError(
                f"Unknown {kind} check: {check!r}. "
                "Valid: 'count', 'contains_xml_ids', 'exact_xml_ids'"
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_target(self, target: str) -> Any:
        """Look *target* up in :attr:`registry`."""
        if target not in self.registry:
            raise YamlConfigurationError(
                f"Target {target!r} not found in registry. Available: {sorted(self.registry)}"
            )
        return self.registry[target]

    def _build_env(self, step: Dict[str, Any], record: Any = None) -> Any:
        """Return an Odoo ``env`` honoring ``context`` / ``as_user`` keys."""
        env = self.env
        as_user = step.get("as_user")
        if as_user:
            user = self.env.ref(as_user)
            env = env(user=user.id)
        ctx = step.get("context")
        if ctx:
            resolved_ctx = self._resolve_value_recursive(ctx, field_type=None)
            env = env(context=dict(env.context, **resolved_ctx))
        return env

    @staticmethod
    def _require(step: Dict[str, Any], key: str, expected_type: Optional[type] = None) -> Any:
        if key not in step:
            raise YamlConfigurationError(
                f"Step {step.get('step')!r} (action={step.get('action')!r}) "
                f"is missing required key {key!r}"
            )
        value = step[key]
        if expected_type is not None and not isinstance(value, expected_type):
            raise YamlConfigurationError(
                f"Step {step.get('step')!r} key {key!r} must be "
                f"{expected_type.__name__}, got {type(value).__name__}"
            )
        return value

    # ------------------------------------------------------------------
    # Dynamic value resolution
    # ------------------------------------------------------------------

    def _resolve_values(self, values: Dict[str, Any], model: Any) -> Dict[str, Any]:
        """Resolve a ``values`` dict against *model*'s field definitions."""
        resolved: dict[str, Any] = {}
        fields_get = model.fields_get(list(values.keys()), attributes=["type"])
        for field_name, raw_value in values.items():
            field_type = fields_get.get(field_name, {}).get("type")
            resolved[field_name] = self._resolve_value_recursive(raw_value, field_type)
        return resolved

    def _resolve_value_recursive(self, value: Any, field_type: Optional[str]) -> Any:
        """Recursively resolve *value*, honoring prefixes and nesting."""
        if isinstance(value, str):
            return self._parse_dynamic_value(value, field_type)
        if isinstance(value, dict):
            return {key: self._resolve_value_recursive(sub, None) for key, sub in value.items()}
        if isinstance(value, list):
            return [self._resolve_value_recursive(item, None) for item in value]
        return value

    def _parse_dynamic_value(self, value: str, field_type: Optional[str]) -> Any:
        """Apply the prefix rules to a single string *value*."""
        if value.startswith("EVAL:"):
            expression = value[len("EVAL:") :].strip()
            return safe_eval(
                expression,
                {"self": self, "env": self.env, "registry": self.registry},
            )
        if value.startswith("REF:"):
            xml_id = value[len("REF:") :].strip()
            return self.env.ref(xml_id).id
        if value.startswith("RECORDSET:"):
            xml_id = value[len("RECORDSET:") :].strip()
            return self.env.ref(xml_id)

        # Implicit xml_id resolution: only when the field is relational AND
        # the string actually looks like a module-prefixed xml_id.
        if field_type in _RELATIONAL_TYPES and _XML_ID_RE.match(value) and "." in value:
            return self.env.ref(value).id

        return value
