"""Main :class:`YamlTransactionCase` implementation.

This module imports Odoo lazily so the rest of the package remains
importable in environments where Odoo is not installed (CI linting,
unit-testing the evaluator, etc.).
"""

import inspect
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterator, List, Optional, Tuple, Type

from .evaluator import safe_eval
from .exceptions import YamlAssertionError, YamlConfigurationError, YamlStepError
from .loader import (
    extract_options,
    extract_setup_steps,
    load_yaml_file,
    validate_scenarios_document,
)

_LOGGER = logging.getLogger("odoo_yaml_test")

#: Regex matching ``module.xml_id`` style strings. Conservative: requires a
#: lowercase module prefix to limit false positives.
_XML_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[A-Za-z0-9_.]+$")

#: Regex matching a ``REC:`` path: an alias followed by a plain getattr chain.
_REC_PATH_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

#: Relational ORM field types.
_RELATIONAL_TYPES: FrozenSet[str] = frozenset({"many2one", "one2many", "many2many", "reference"})

#: The x2many subset of the above; these take ORM command triples.
_X2MANY_TYPES: FrozenSet[str] = frozenset({"one2many", "many2many"})

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

#: Marker set on an exception once a step-context prefix has been added, so a
#: nested run (setup steps, sub-scenarios) never wraps the same error twice.
_CONTEXT_MARKER = "_odoo_yaml_test_contextualized"

#: Stdlib exceptions an ``expect_error`` step may name. Odoo's own exceptions
#: are added lazily by :func:`_error_class` — we never getattr an arbitrary
#: dotted path, which would undo the security posture of ``evaluator.py``.
_STDLIB_ERRORS: Dict[str, Type[BaseException]] = {
    "AssertionError": AssertionError,
    "AttributeError": AttributeError,
    "KeyError": KeyError,
    "TypeError": TypeError,
    "ValueError": ValueError,
}

#: Odoo exception names resolved from ``odoo.exceptions`` on first use.
_ODOO_ERROR_NAMES: Tuple[str, ...] = (
    "AccessDenied",
    "AccessError",
    "CacheMiss",
    "MissingError",
    "RedirectWarning",
    "UserError",
    "ValidationError",
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


def _error_class(name: str) -> Type[BaseException]:
    """Resolve an exception *name* to a class from a fixed whitelist.

    Raises:
        YamlConfigurationError: when *name* is not a known exception.
    """
    if name in _STDLIB_ERRORS:
        return _STDLIB_ERRORS[name]
    if name in _ODOO_ERROR_NAMES:
        try:
            # Lazy on purpose: Odoo stays an optional import (see module docstring).
            import odoo.exceptions as odoo_exceptions
        except ImportError as exc:  # pragma: no cover - only without Odoo
            raise YamlConfigurationError(
                f"expect_error type {name!r} requires Odoo to be importable"
            ) from exc
        resolved = getattr(odoo_exceptions, name, None)
        if isinstance(resolved, type) and issubclass(resolved, BaseException):
            return resolved
    known = sorted(set(_STDLIB_ERRORS) | set(_ODOO_ERROR_NAMES))
    raise YamlConfigurationError(f"Unknown expect_error type {name!r}. Known: {known}")


class _FormProxy:
    """Adapts an Odoo ``Form`` so the ``_assert_*`` methods can read it.

    The assertion helpers only need ``_name`` (for error messages) and
    attribute access; a Form supports the latter natively.
    """

    def __init__(self, form: Any, model_name: str) -> None:
        # Bypass our own __getattr__ for these two.
        object.__setattr__(self, "_form", form)
        object.__setattr__(self, "_name", model_name)

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "_form"), name)


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

    #: Opt-in: flush + invalidate a record's cache before asserting on it.
    #:
    #: Default ``False``, and deliberately so. Invalidating forces the field to
    #: be re-read from the database, and a re-read runs the record rules that a
    #: cached value never had to pass. Turning this on therefore surfaces latent
    #: security-rule bugs — asserts that only ever passed because nobody re-read
    #: the value. Measured against ssi_school: 15 of 79 tests turn into
    #: AccessError, because the scenarios act `as_user: base.user_admin` on
    #: records that user cannot actually read.
    #:
    #: Those tests are wrong and should be fixed — but that is a decision each
    #: module owner makes on their own schedule, not something a library upgrade
    #: should force. Enable per file with ``options: {auto_refresh: true}``, per
    #: scenario, per step (``refresh: true``), or here on a subclass.
    auto_refresh = False

    #: Seam for injecting a Form implementation in tests. When ``None`` the
    #: ``form`` action imports ``odoo.tests.common.Form`` lazily.
    form_class: Any = None

    def setUp(self) -> None:
        super().setUp()
        self._reset_registry()

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def run_yaml_scenario(self, filename: str) -> None:
        """Execute every scenario declared in *filename*.

        The file is resolved relative to the directory of the test
        class's source file via :func:`inspect.getfile`. Each scenario
        runs inside its own ``subTest`` so a failure in one does not
        block the others, and against a freshly reset registry.

        When the document declares a top-level ``setup:`` block, its
        steps are replayed before *each* scenario, so scenarios share
        fixtures without sharing state.

        Args:
            filename: Name (or relative path) of the YAML file.
        """
        yaml_path = self._resolve_yaml_path(filename)
        source = str(yaml_path)
        document = load_yaml_file(yaml_path)
        scenarios = validate_scenarios_document(document, source)
        setup_steps = extract_setup_steps(document, source)
        file_options = extract_options(document, source)

        for scenario in scenarios:
            scenario_name = scenario["name"]
            with self.subTest(scenario=scenario_name, file=source):
                self._reset_registry()
                self._options = dict(file_options, **scenario.get("options", {}))
                if setup_steps and not scenario.get("skip_setup"):
                    self._run_steps(setup_steps, source, scenario_name, phase="Setup Step")
                self._run_steps(scenario["steps"], source, scenario_name, phase="Step")

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

    def _reset_registry(self) -> None:
        """Start a fresh registry, seeded with the built-in aliases."""
        self.registry = {}
        env = getattr(self, "env", None)
        if env is not None:
            for alias in ("company", "user"):
                value = getattr(env, alias, None)
                if value is not None:
                    self.registry[alias] = value

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

    @staticmethod
    def _step_context(
        yaml_file: str, scenario_name: str, step_name: str, action: Any, phase: str
    ) -> str:
        """Build the ``Error in File: ... -> Scenario: ...`` prefix."""
        return (
            f"Error in File: {yaml_file} -> Scenario: {scenario_name!r} "
            f"-> {phase}: {step_name!r} (action={action}): "
        )

    def _run_steps(
        self,
        steps: List[Dict[str, Any]],
        yaml_file: str,
        scenario_name: str,
        phase: str = "Step",
    ) -> None:
        """Execute *steps*, wrapping any failure with its location."""
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                raise YamlConfigurationError(
                    f"{phase} at index {index} in {yaml_file} must be a mapping"
                )
            step_name = step.get("step", f"<step {index}>")
            action = step.get("action")
            _LOGGER.info("[%s::%s::%s] action=%s", yaml_file, scenario_name, step_name, action)
            try:
                self._dispatch_step(step)
            except Exception as exc:
                if getattr(exc, _CONTEXT_MARKER, False):
                    raise
                _LOGGER.error("[%s::%s::%s] failed: %s", yaml_file, scenario_name, step_name, exc)
                prefix = self._step_context(yaml_file, scenario_name, step_name, action, phase)
                # Assertion failures must stay AssertionErrors so unittest
                # reports them as failures, not errors. Everything else
                # becomes a YamlStepError.
                if isinstance(exc, YamlAssertionError):
                    wrapped: Exception = YamlAssertionError(f"{prefix}{exc}")
                else:
                    wrapped = YamlStepError(f"{prefix}{type(exc).__name__}: {exc}")
                setattr(wrapped, _CONTEXT_MARKER, True)
                raise wrapped from exc

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
    # Expected errors
    # ------------------------------------------------------------------

    @contextmanager
    def _expecting(self, step: Dict[str, Any]) -> Iterator[None]:
        """Run the wrapped block under this step's ``expect_error`` spec.

        Without ``expect_error`` this is a plain pass-through. With it, the
        block must raise the named exception; the block runs inside a
        savepoint so a failed ``ValidationError`` does not poison the rest
        of the transaction.
        """
        spec = step.get("expect_error")
        if not spec:
            yield
            return

        if not isinstance(spec, dict):
            raise YamlConfigurationError(
                f"'expect_error' must be a mapping, got {type(spec).__name__}"
            )
        if step.get("save_as"):
            raise YamlConfigurationError(
                "'expect_error' cannot be combined with 'save_as': the step is "
                "expected to fail, so there is no record to save"
            )
        type_name = spec.get("type")
        if not type_name:
            raise YamlConfigurationError("'expect_error' requires a 'type'")
        expected_cls = _error_class(type_name)
        needle = spec.get("message_contains")

        raised: Optional[BaseException] = None
        try:
            # The savepoint must SEE the exception so it rolls back, hence the
            # catch sits outside it. Without the rollback a ValidationError
            # would leave the cursor aborted and poison every later step.
            with self._savepoint():
                yield
        except expected_cls as exc:
            raised = exc
        # Any other exception propagates untouched: the normal step wrapper
        # then reports what actually happened, which beats us claiming
        # "wrong exception type".

        if raised is None:
            raise YamlAssertionError(f"expected {type_name} to be raised, but nothing was raised")
        if needle and needle not in str(raised):
            raise YamlAssertionError(
                f"expected {type_name} message to contain {needle!r}, got {str(raised)!r}"
            )

    @contextmanager
    def _savepoint(self) -> Iterator[None]:
        """Wrap the block in a cursor savepoint when one is available.

        Odoo's ``cr.savepoint()`` rolls back and re-raises on error, which is
        exactly what an ``expect_error`` step needs.
        """
        cr = getattr(getattr(self, "env", None), "cr", None)
        savepoint = getattr(cr, "savepoint", None)
        if savepoint is None:
            yield
            return
        with savepoint():
            yield

    # ------------------------------------------------------------------
    # Action handlers
    # ------------------------------------------------------------------

    def _action_create(self, step: Dict[str, Any]) -> None:
        """Handle ``action: create``."""
        model_name = self._require(step, "model")
        values = self._require(step, "values", expected_type=dict)
        env = self._build_env(step)
        model = env[model_name]
        record = None
        with self._expecting(step):
            resolved_values = self._resolve_values(values, model)
            record = model.create(resolved_values)
        save_as = step.get("save_as")
        if save_as and record is not None:
            self.registry[save_as] = record

    def _action_write(self, step: Dict[str, Any]) -> None:
        """Handle ``action: write``."""
        target = self._require(step, "target")
        values = self._require(step, "values", expected_type=dict)
        record = self._resolve_target(target)
        record_in_env = self._rebind(record, step)
        with self._expecting(step):
            resolved_values = self._resolve_values(values, record_in_env)
            record_in_env.write(resolved_values)

    def _action_call(self, step: Dict[str, Any]) -> None:
        """Handle ``action: call``."""
        target = self._require(step, "target")
        method_name = self._require(step, "method")
        record = self._resolve_target(target)
        record_in_env = self._rebind(record, step)

        args = self._resolve_value_recursive(step.get("args", []), field_type=None)
        kwargs = self._resolve_value_recursive(step.get("kwargs", {}), field_type=None)
        if not isinstance(args, list):
            raise YamlConfigurationError(f"'args' must be a list, got {type(args).__name__}")
        if not isinstance(kwargs, dict):
            raise YamlConfigurationError(f"'kwargs' must be a mapping, got {type(kwargs).__name__}")

        with self._expecting(step):
            method = getattr(record_in_env, method_name)
            method(*args, **kwargs)

        asserts = step.get("asserts")
        if asserts:
            self._run_asserts(record_in_env, asserts, step)

    def _action_unlink(self, step: Dict[str, Any]) -> None:
        """Handle ``action: unlink``."""
        target = self._require(step, "target")
        record = self._resolve_target(target)
        record_in_env = self._rebind(record, step)
        with self._expecting(step):
            record_in_env.unlink()

    def _action_assert(self, step: Dict[str, Any]) -> None:
        """Handle ``action: assert``."""
        target = self._require(step, "target")
        asserts = self._require(step, "asserts", expected_type=dict)
        record = self._resolve_target(target)
        self._run_asserts(self._rebind(record, step), asserts, step)

    def _action_ref(self, step: Dict[str, Any]) -> None:
        """Handle ``action: ref``."""
        xml_id = self._require(step, "xml_id")
        save_as = self._require(step, "save_as")
        self.registry[save_as] = self._build_env(step).ref(xml_id)

    def _action_search(self, step: Dict[str, Any]) -> None:
        """Handle ``action: search``."""
        model_name = self._require(step, "model")
        domain = self._require(step, "domain", expected_type=list)
        save_as = self._require(step, "save_as")
        env = self._build_env(step)
        model = env[model_name]

        if self._should_refresh(step):
            self._refresh(model)

        resolved_domain = self._resolve_value_recursive(domain, field_type=None)
        kwargs: Dict[str, Any] = {}
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

    def _action_wizard(self, step: Dict[str, Any]) -> None:
        """Handle ``action: wizard``.

        Creates a transient model the way the UI does — with
        ``active_model`` / ``active_id`` / ``active_ids`` in the context —
        then optionally calls a method on it. This is what makes SSI's
        cancel/terminate reason wizards reachable from YAML.
        """
        model_name = self._require(step, "model")
        # NB: the target key is 'target', not 'on' — YAML 1.1 parses a bare
        # `on:` key as the boolean True.
        target = self._resolve_target(self._require(step, "target"))

        # The wizard context is a default: an explicit `context:` wins.
        wizard_ctx = {
            "active_model": target._name,
            "active_id": target.id,
            "active_ids": list(target.ids),
        }
        env = self._build_env(step, base_context=wizard_ctx)
        model = env[model_name]

        values = step.get("values") or {}
        if not isinstance(values, dict):
            raise YamlConfigurationError(f"'values' must be a mapping, got {type(values).__name__}")

        wizard = None
        method_name = step.get("method")
        with self._expecting(step):
            wizard = model.create(self._resolve_values(values, model))
            if method_name:
                getattr(wizard, method_name)()

        save_as = step.get("save_as")
        if save_as and wizard is not None:
            self.registry[save_as] = wizard

        asserts = step.get("asserts")
        if asserts:
            # Assert on the *document*, not the transient — that is what the
            # author cares about after a cancel wizard runs.
            self._run_asserts(self._rebind(target, step), asserts, step)

    def _action_form(self, step: Dict[str, Any]) -> None:
        """Handle ``action: form`` — drive Odoo's ``Form`` API.

        This is the only way to exercise ``@api.onchange``: onchange methods
        do not run through plain ``create()`` / ``write()``.
        """
        form_cls = self._get_form_class()
        target_alias = step.get("target")
        model_name = step.get("model")
        if bool(target_alias) == bool(model_name):
            raise YamlConfigurationError(
                "'form' requires exactly one of 'model' (new record) or 'target' (edit existing)"
            )

        env = self._build_env(step)
        if target_alias:
            record = self._rebind(self._resolve_target(target_alias), step)
            subject: Any = record
            model_label = record._name
        else:
            subject = env[model_name]
            model_label = model_name

        ops = self._form_ops(step)
        save = step.get("save", True)
        save_as = step.get("save_as")
        if save_as and not save:
            raise YamlConfigurationError("'save_as' requires 'save: true' — nothing is created")

        saved = None
        with self._expecting(step):
            form = form_cls(subject)
            proxy = _FormProxy(form, model_label)
            for op in ops:
                self._apply_form_op(form, proxy, op)
            asserts = step.get("asserts")
            if asserts:
                # Assert against the pending form state, pre-save. No refresh:
                # the Form *is* the pending state, there is no cache to drop.
                self._run_asserts(proxy, asserts, step, refresh=False)
            if save:
                saved = form.save()

        if save_as and saved is not None:
            self.registry[save_as] = saved

    # ------------------------------------------------------------------
    # Form helpers
    # ------------------------------------------------------------------

    def _get_form_class(self) -> Any:
        """Return the Form class, preferring the injected test seam."""
        if self.form_class is not None:
            return self.form_class
        try:
            # Lazy on purpose: Odoo stays an optional import (see module docstring).
            from odoo.tests.common import Form
        except ImportError as exc:  # pragma: no cover - only without Odoo
            raise YamlConfigurationError(
                "action 'form' requires Odoo to be importable (odoo.tests.common.Form)"
            ) from exc
        return Form

    def _form_ops(self, step: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Normalize ``values:`` / ``ops:`` into one ordered op list.

        ``values:`` is sugar for a sequence of ``set`` ops in YAML order
        (``yaml.safe_load`` preserves mapping order on Python 3.7+).
        """
        values = step.get("values")
        ops = step.get("ops")
        if values is not None and ops is not None:
            raise YamlConfigurationError("'form' accepts either 'values' or 'ops', not both")
        if ops is not None:
            if not isinstance(ops, list):
                raise YamlConfigurationError(f"'ops' must be a list, got {type(ops).__name__}")
            return ops
        if values is not None:
            if not isinstance(values, dict):
                raise YamlConfigurationError(
                    f"'values' must be a mapping, got {type(values).__name__}"
                )
            return [{"set": {name: value}} for name, value in values.items()]
        return []

    def _apply_form_op(self, form: Any, proxy: _FormProxy, op: Dict[str, Any]) -> None:
        """Apply one ``set`` / ``new`` / ``edit`` / ``remove`` / ``assert`` op."""
        if not isinstance(op, dict) or len(op) != 1:
            raise YamlConfigurationError(f"Each form op must be a single-key mapping, got {op!r}")
        ((kind, body),) = op.items()

        if kind == "set":
            if not isinstance(body, dict):
                raise YamlConfigurationError("form 'set' takes a mapping of field -> value")
            for field_name, raw in body.items():
                # Form assignment wants the RECORD for relational fields, not
                # an id — so values are resolved without field-type coercion.
                setattr(form, field_name, self._resolve_value_recursive(raw, field_type=None))
        elif kind == "assert":
            if not isinstance(body, dict):
                raise YamlConfigurationError("form 'assert' takes a mapping of field -> spec")
            self._run_asserts(proxy, body, {}, refresh=False)
        elif kind in ("new", "edit"):
            if not isinstance(body, dict):
                raise YamlConfigurationError(f"form {kind!r} takes a mapping")
            field_name = body.get("field")
            if not field_name:
                raise YamlConfigurationError(f"form {kind!r} requires 'field'")
            values = body.get("values") or {}
            x2m = getattr(form, field_name)
            if kind == "new":
                ctx = x2m.new()
            else:
                if "index" not in body:
                    raise YamlConfigurationError("form 'edit' requires 'index'")
                ctx = x2m.edit(body["index"])
            with ctx as line:
                for sub_field, raw in values.items():
                    setattr(line, sub_field, self._resolve_value_recursive(raw, field_type=None))
        elif kind == "remove":
            if not isinstance(body, dict) or "field" not in body or "index" not in body:
                raise YamlConfigurationError("form 'remove' requires 'field' and 'index'")
            getattr(form, body["field"]).remove(index=body["index"])
        else:
            raise YamlConfigurationError(
                f"Unknown form op {kind!r}. Valid: 'set', 'new', 'edit', 'remove', 'assert'"
            )

    # ------------------------------------------------------------------
    # Cache refresh
    # ------------------------------------------------------------------

    def _should_refresh(self, step: Dict[str, Any]) -> bool:
        """Decide whether to refresh before reading fields for *step*.

        Precedence: per-step ``refresh:`` > per-scenario/file ``options:
        {auto_refresh: ...}`` > the class attribute ``auto_refresh``.
        """
        if "refresh" in step:
            return bool(step["refresh"])
        options = getattr(self, "_options", None) or {}
        if "auto_refresh" in options:
            return bool(options["auto_refresh"])
        return bool(self.auto_refresh)

    @staticmethod
    def _refresh(record: Any) -> None:
        """Flush pending writes and drop *record*'s cache before reading it.

        SSI's policy fields (``confirm_ok``, ``approve_ok``, …) are
        non-stored computes that Odoo does not invalidate when ``state``
        changes, so without this an assert right after a state transition
        reads a stale value.

        The invalidation is deliberately **scoped to this record's ids**.
        Odoo's ``invalidate_cache()`` with no arguments empties the cache for
        the *whole environment*, which forces unrelated records to be re-read
        from the database — and a re-read runs record rules that the cached
        value never had to pass. That turns a passing assert into an
        AccessError in scenarios where an earlier step ran ``as_user``.
        Scoping keeps the fix (fresh computes on the record under assertion)
        without the collateral damage.
        """
        flush = getattr(record, "flush", None)
        if callable(flush):
            flush()

        invalidate = getattr(record, "invalidate_cache", None)
        if not callable(invalidate):
            return
        ids = getattr(record, "ids", None)
        if ids:
            invalidate(ids=list(ids))
        # An empty recordset (e.g. the model used by `search`) has nothing to
        # invalidate; the flush above is what makes the search see prior writes.

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------

    def _run_asserts(
        self,
        record: Any,
        asserts: Dict[str, Any],
        step: Optional[Dict[str, Any]] = None,
        refresh: Optional[bool] = None,
    ) -> None:
        """Run a mapping of ``field_path -> spec`` assertions on *record*."""
        if not isinstance(asserts, dict):
            raise YamlConfigurationError(
                f"'asserts' must be a mapping, got {type(asserts).__name__}"
            )

        if refresh is None:
            refresh = self._should_refresh(step or {})
        if refresh:
            self._refresh(record)

        for field_path, spec in asserts.items():
            if not isinstance(spec, dict):
                raise YamlConfigurationError(f"Assert spec for {field_path!r} must be a mapping")
            resolved_spec = self._resolve_assert_spec(spec)
            assert_type = resolved_spec.get("type", "value")
            method = getattr(self, f"_assert_{assert_type}", None)
            if method is None:
                raise YamlConfigurationError(f"Unknown assert type: {assert_type!r}")
            method(record, field_path, resolved_spec)

    def _resolve_assert_spec(self, spec: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve dynamic prefixes inside an assert spec's data keys.

        ``type``, ``operator`` and ``check`` are schema keywords and are left
        alone. Everything else may carry ``REC:`` / ``REF:`` / ``EVAL:`` /
        ``RECORDSET:``. Resolution runs with ``field_type=None`` so a plain
        ``expected: "sale"`` can never be mistaken for an xml_id.
        """
        keywords = ("type", "operator", "check")
        resolved: Dict[str, Any] = {}
        for key, value in spec.items():
            if key in keywords:
                resolved[key] = value
            else:
                resolved[key] = self._resolve_value_recursive(value, field_type=None)
        return resolved

    @staticmethod
    def _read_path(record: Any, path: str) -> Any:
        """Read a possibly dotted *path* off *record*.

        No Odoo field name contains a dot, so a dot always means traversal.
        """
        current = record
        walked: List[str] = []
        for segment in path.split("."):
            if current is None or (hasattr(current, "__len__") and len(current) == 0):
                joined = ".".join(walked) or "<record>"
                raise YamlAssertionError(f"cannot read {segment!r}: {joined} is empty")
            try:
                current = getattr(current, segment)
            except AttributeError as exc:
                joined = ".".join(walked) or getattr(record, "_name", "<record>")
                raise YamlConfigurationError(
                    f"{joined} has no field {segment!r} (reading path {path!r})"
                ) from exc
            walked.append(segment)
        return current

    def _coerce_to_ids(self, value: Any) -> List[int]:
        """Normalize a record / id / list-of-either into a list of ids."""
        if value is None or value is False:
            return []
        if isinstance(value, int):
            return [value]
        ids = getattr(value, "ids", None)
        if ids is not None:
            return list(ids)
        if isinstance(value, (list, tuple, set)):
            result: List[int] = []
            for item in value:
                result.extend(self._coerce_to_ids(item))
            return result
        raise YamlConfigurationError(
            f"Cannot interpret {value!r} ({type(value).__name__}) as a record reference"
        )

    def _assert_value(self, record: Any, field_path: str, spec: Dict[str, Any]) -> None:
        """``type: value`` — comparison-operator assertion."""
        operator = spec.get("operator", "equals")
        if operator not in _VALID_OPERATORS:
            raise YamlConfigurationError(
                f"Unknown operator {operator!r}. Valid: {sorted(_VALID_OPERATORS)}"
            )
        actual = self._read_path(record, field_path)
        expected = spec.get("expected")
        label = f"{getattr(record, '_name', '<record>')}.{field_path}"

        if operator == "equals" and actual != expected:
            raise YamlAssertionError(f"{label}: expected {expected!r}, got {actual!r}")
        if operator == "not_equals" and actual == expected:
            raise YamlAssertionError(f"{label}: expected != {expected!r}, but equal")
        if operator == "gt" and not actual > expected:
            raise YamlAssertionError(f"{label}: expected > {expected!r}, got {actual!r}")
        if operator == "gte" and not actual >= expected:
            raise YamlAssertionError(f"{label}: expected >= {expected!r}, got {actual!r}")
        if operator == "lt" and not actual < expected:
            raise YamlAssertionError(f"{label}: expected < {expected!r}, got {actual!r}")
        if operator == "lte" and not actual <= expected:
            raise YamlAssertionError(f"{label}: expected <= {expected!r}, got {actual!r}")
        if operator == "in":
            if expected is None:
                raise YamlConfigurationError(
                    f"'in' operator on {field_path!r} requires 'expected' to be a container"
                )
            if actual not in expected:
                raise YamlAssertionError(f"{label}: {actual!r} not in {expected!r}")
        if operator == "not_in":
            if expected is None:
                raise YamlConfigurationError(
                    f"'not_in' operator on {field_path!r} requires 'expected' to be a container"
                )
            if actual in expected:
                raise YamlAssertionError(f"{label}: {actual!r} unexpectedly in {expected!r}")
        if operator == "contains" and expected not in actual:
            raise YamlAssertionError(f"{label}: {expected!r} not in {actual!r}")
        if operator == "is_truthy" and not actual:
            raise YamlAssertionError(f"{label}: expected truthy, got {actual!r}")
        if operator == "is_falsy" and actual:
            raise YamlAssertionError(f"{label}: expected falsy, got {actual!r}")

    def _assert_m2o(self, record: Any, field_path: str, spec: Dict[str, Any]) -> None:
        """``type: m2o`` — compare the related id against a record or xml_id."""
        has_expected = "expected" in spec
        expected_xml_id = spec.get("expected_xml_id")
        if has_expected and expected_xml_id:
            raise YamlConfigurationError(
                f"m2o assert on {field_path!r}: use either 'expected' or "
                "'expected_xml_id', not both"
            )
        if has_expected:
            expected_ids = self._coerce_to_ids(spec["expected"])
        elif expected_xml_id:
            expected_ids = [self.env.ref(expected_xml_id).id]
        else:
            raise YamlConfigurationError(
                f"m2o assert on {field_path!r} requires 'expected' or 'expected_xml_id'"
            )

        actual = self._read_path(record, field_path)
        actual_ids = self._coerce_to_ids(actual)
        label = f"{getattr(record, '_name', '<record>')}.{field_path}"
        if actual_ids != expected_ids:
            raise YamlAssertionError(f"{label}: expected id {expected_ids}, got {actual_ids}")

    def _assert_o2m(self, record: Any, field_path: str, spec: Dict[str, Any]) -> None:
        """``type: o2m`` — count or membership assertion."""
        self._assert_relation(record, field_path, spec, kind="o2m")

    def _assert_m2m(self, record: Any, field_path: str, spec: Dict[str, Any]) -> None:
        """``type: m2m`` — count or membership assertion."""
        self._assert_relation(record, field_path, spec, kind="m2m")

    def _assert_relation(
        self, record: Any, field_path: str, spec: Dict[str, Any], kind: str
    ) -> None:
        check = spec.get("check", "count")
        recordset = self._read_path(record, field_path)
        label = f"{getattr(record, '_name', '<record>')}.{field_path}"

        if check == "count":
            expected_count = spec.get("expected_count")
            if expected_count is None:
                raise YamlConfigurationError(
                    f"{kind} count check on {field_path!r} requires 'expected_count'"
                )
            if len(recordset) != expected_count:
                raise YamlAssertionError(
                    f"{label}: expected {expected_count} records, got {len(recordset)}"
                )
            return

        # Membership checks. The xml_id-only spellings are kept as thin
        # wrappers over the same id comparison.
        if check in ("contains", "exact"):
            expected_ids = set(self._coerce_to_ids(spec.get("expected_records") or []))
        elif check in ("contains_xml_ids", "exact_xml_ids"):
            expected_ids = {self.env.ref(xid).id for xid in (spec.get("expected_xml_ids") or [])}
        else:
            raise YamlConfigurationError(
                f"Unknown {kind} check: {check!r}. Valid: 'count', 'contains', 'exact', "
                "'contains_xml_ids', 'exact_xml_ids'"
            )

        actual_ids = set(self._coerce_to_ids(recordset))
        subset = check in ("contains", "contains_xml_ids")
        if subset and not expected_ids.issubset(actual_ids):
            missing = expected_ids - actual_ids
            raise YamlAssertionError(f"{label}: missing ids {missing} from {actual_ids}")
        if not subset and expected_ids != actual_ids:
            raise YamlAssertionError(
                f"{label}: expected exact ids {expected_ids}, got {actual_ids}"
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

    def _rebind(self, record: Any, step: Dict[str, Any]) -> Any:
        """Return *record* bound to the env implied by the step's keys."""
        env = self._build_env(step, record=record)
        if env is record.env:
            return record
        return record.with_env(env)

    def _build_env(
        self,
        step: Dict[str, Any],
        record: Any = None,
        base_context: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Return an Odoo ``env`` honoring ``context`` / ``as_user`` keys."""
        env = self.env
        as_user = step.get("as_user")
        if as_user:
            env = env(user=self._resolve_user(as_user).id)
        ctx = step.get("context")
        extra: Dict[str, Any] = dict(base_context or {})
        if ctx:
            resolved_ctx = self._resolve_value_recursive(ctx, field_type=None)
            if not isinstance(resolved_ctx, dict):
                raise YamlConfigurationError(
                    f"'context' must be a mapping, got {type(resolved_ctx).__name__}"
                )
            # Explicit context wins over any caller-supplied defaults.
            extra.update(resolved_ctx)
        if extra:
            env = env(context=dict(env.context, **extra))
        return env

    def _resolve_user(self, as_user: str) -> Any:
        """Resolve an ``as_user`` value to a ``res.users`` record.

        Accepts an xml_id (the historical form), any dynamic prefix, or a
        bare registry alias — so a user created earlier in the scenario can
        be used without inventing an xml_id for it.
        """
        if not isinstance(as_user, str):
            raise YamlConfigurationError(
                f"'as_user' must be a string, got {type(as_user).__name__}"
            )
        for prefix in ("EVAL:", "REF:", "RECORDSET:", "REC:"):
            if as_user.startswith(prefix):
                resolved = self._parse_dynamic_value(as_user, field_type=None)
                if isinstance(resolved, int):
                    return self.env["res.users"].browse(resolved)
                return resolved
        if as_user in self.registry:
            return self.registry[as_user]
        if "." in as_user:
            return self.env.ref(as_user)
        raise YamlConfigurationError(
            f"'as_user' {as_user!r} is neither a registry alias nor an xml_id. "
            f"Available aliases: {sorted(self.registry)}"
        )

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
        resolved: Dict[str, Any] = {}
        fields_get = model.fields_get(list(values.keys()), attributes=["type"])
        for field_name, raw_value in values.items():
            field_type = fields_get.get(field_name, {}).get("type")
            value = self._resolve_value_recursive(raw_value, field_type)
            resolved[field_name] = self._coerce_for_field(value, field_type)
        return resolved

    def _coerce_for_field(self, value: Any, field_type: Optional[str]) -> Any:
        """Turn a recordset into what *field_type* expects in ``create``/``write``.

        Only applied where the field type is known — never inside domains,
        args, or kwargs, where an ORM command triple would be nonsense.
        """
        if field_type not in _RELATIONAL_TYPES:
            return value
        ids = getattr(value, "ids", None)
        if ids is None:
            return value
        if field_type in _X2MANY_TYPES:
            return [(6, 0, list(ids))]
        return ids[0] if ids else False

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
        if value.startswith("REC:"):
            return self._resolve_rec_path(value[len("REC:") :].strip())
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

    def _resolve_rec_path(self, path: str) -> Any:
        """Resolve a ``REC:`` path: a registry alias plus a getattr chain.

        ``REC: so_1`` yields the record itself; ``REC: so_1.id`` yields its
        integer id; ``REC: so_1.partner_id.name`` walks the relation. There
        are no calls, subscripts, or operators — for those, use ``EVAL:``.
        """
        if not _REC_PATH_RE.match(path):
            raise YamlConfigurationError(
                f"Invalid REC path {path!r}: expected an alias followed by dotted "
                "attribute names (no calls, indexes, or operators — use EVAL: for those)"
            )
        alias, _, rest = path.partition(".")
        if alias not in self.registry:
            raise YamlConfigurationError(
                f"REC alias {alias!r} not found in registry. Available: {sorted(self.registry)}"
            )
        current = self.registry[alias]
        walked = [alias]
        for segment in rest.split(".") if rest else []:
            try:
                current = getattr(current, segment)
            except AttributeError as exc:
                raise YamlConfigurationError(
                    f"REC path {path!r}: {'.'.join(walked)} has no attribute {segment!r}"
                ) from exc
            walked.append(segment)
        return current
