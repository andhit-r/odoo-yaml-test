"""Smoke tests for case.py without a live Odoo install.

We construct a fake ``env``/``model``/``record`` graph that mimics the
public ORM API surface used by the YamlTransactionCase. This lets us
verify dispatch logic, error wrapping, scenario isolation, and dynamic
value resolution end-to-end without needing a Postgres database.

These tests instantiate the YamlTransactionCase placeholder directly
through ``object.__new__`` to bypass the unittest setUp machinery,
because we are not actually running an Odoo TransactionCase.

The fakes model one behaviour deliberately: ``FakeRecord`` keeps a
``_cache`` that goes stale on ``write``, mirroring the real reason SSI
scenarios are littered with manual cache-invalidation steps (non-stored
compute fields that Odoo does not invalidate on a state change). That is
what lets us prove the auto-refresh feature actually fires.

The fakes speak the **Odoo 19.0** cache API — ``env.flush_all()`` and
``invalidate_recordset()`` — because that is what this branch's ``case.py``
calls. Keeping them aligned with the branch's target series is the point: a
fake that answered to the 14.0 names (``flush``/``invalidate_cache``) would
be describing an ORM that does not exist here, and would keep passing while
the real thing broke. These fakes can never prove the library works against
a real Odoo 19 — only the ``integration`` CI job can (see
``tests_integration/``).
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar, Iterator
from unittest import mock

import pytest

from odoo_yaml_test import case as case_module
from odoo_yaml_test.case import YamlTransactionCase
from odoo_yaml_test.exceptions import (
    YamlAssertionError,
    YamlConfigurationError,
    YamlStepError,
)

# ----------------------------------------------------------------------
# Fake Odoo ORM
# ----------------------------------------------------------------------


class FakeRecord:
    """Minimal stand-in for an Odoo recordset of length 1."""

    _id_counter = 0

    def __init__(self, model: FakeModel, values: dict[str, Any]) -> None:
        FakeRecord._id_counter += 1
        self.id = FakeRecord._id_counter
        self._name = model.name
        self._model = model
        self._values = dict(values)
        # Values as seen through the ORM cache. Goes stale on write, exactly
        # like a non-stored compute field whose dependency changed.
        self._cache: dict[str, Any] = {}
        self.env = model.env

    def __getattr__(self, name: str) -> Any:
        # Only reached when normal attribute lookup fails.
        cache = self.__dict__.get("_cache", {})
        if name in cache:
            return cache[name]
        values = self.__dict__.get("_values", {})
        if name in values:
            return values[name]
        raise AttributeError(name)

    @property
    def ids(self) -> list[int]:
        return [self.id]

    def __len__(self) -> int:
        return 1

    def __iter__(self) -> Iterator[FakeRecord]:
        yield self

    def write(self, values: dict[str, Any]) -> bool:
        self._values.update(values)
        return True

    def unlink(self) -> bool:
        # Written into _values, which with_env() clones share by reference —
        # real Odoo recordsets pointing at the same row share state too.
        self._values["unlinked"] = True
        return True

    def with_env(self, env: FakeEnv) -> FakeRecord:
        clone = object.__new__(FakeRecord)
        clone.__dict__.update(self.__dict__)
        clone.env = env
        return clone

    # -- cache plumbing, mirroring Odoo 19's public API -----------------

    def invalidate_recordset(self, fnames=None, flush=True) -> None:
        """Mirror ``odoo/orm/models.py::invalidate_recordset`` (19.0).

        Scoped to ``self`` by construction — there is no argument that could
        widen it to the whole environment. That is the improvement over 14.0's
        ``invalidate_cache()``, whose no-argument form swept every record in
        the env, forcing unrelated records to be re-read under a restricted
        user and blowing up with AccessError against real Odoo.

        The env-wide sweep still exists in 19 as ``env.invalidate_all()``; the
        fake keeps it (and its ``cache_sweeps`` counter) so the tests can still
        prove we never reach for it.
        """
        if flush:
            self.flush_recordset(fnames)
        self._cache.clear()

    def flush_recordset(self, fnames=None) -> None:
        """Mirror ``flush_recordset`` (19.0), including its empty-set no-op."""
        if not self.ids:
            return
        self.env.flushed += 1

    # -- methods driven by scenarios -----------------------------------

    def action_confirm(self) -> bool:
        self._values["state"] = "sale"
        return True

    def action_confirm_stale(self) -> bool:
        """Transition state, but leave a stale cached value behind.

        This is the SSI failure mode: reading ``state`` without invalidating
        first yields the pre-transition value.
        """
        self._cache["state"] = self._values.get("state", "draft")
        self._values["state"] = "confirm"
        return True

    def action_boom(self) -> None:
        raise ValueError("cannot confirm: no lines")


class FakeRecordSet:
    """A recordset of arbitrary length, for o2m/m2m and search results."""

    def __init__(self, name: str, records: list[FakeRecord]) -> None:
        self._name = name
        self._records = list(records)

    @property
    def ids(self) -> list[int]:
        return [rec.id for rec in self._records]

    @property
    def id(self) -> int | bool:
        """Mirror Odoo's ``id`` field descriptor, ``ensure_one()`` included.

        ``fields.Id.__get__`` returns ``False`` for an empty recordset, the id
        for a singleton, and raises ``ValueError("Expected singleton")`` above
        that. Without that last branch a regression in ``_action_wizard`` would
        surface here as a bland ``AttributeError`` — or not at all — instead of
        the singleton error real Odoo raises.

        Returns:
            The record's id, or ``False`` when the recordset is empty.

        Raises:
            ValueError: If the recordset holds more than one record.
        """
        if not self._records:
            return False
        if len(self._records) > 1:
            raise ValueError(f"Expected singleton: {self._name}({self.ids})")
        return self._records[0].id

    def __len__(self) -> int:
        return len(self._records)

    def __iter__(self) -> Iterator[FakeRecord]:
        return iter(self._records)


class FakeModel:
    def __init__(self, name: str, env: FakeEnv) -> None:
        self.name = name
        self.env = env
        self._field_types: dict[str, str] = {
            "partner_id": "many2one",
            "product_id": "many2one",
            "order_id": "many2one",
            "user_id": "many2one",
            "cancel_reason_id": "many2one",
            "tag_ids": "many2many",
            "order_line": "one2many",
            "name": "char",
            "note": "text",
            "state": "char",
            "date_order": "datetime",
            "product_uom_qty": "float",
        }

    @property
    def ids(self) -> list[int]:
        """Mirror a real empty recordset: ``env["model"]`` has ``ids == []``.

        ``search`` refreshes through this object, and ``_refresh`` reads
        ``.ids`` to decide whether there is a cache to drop. The fake used to
        omit this entirely and got away with it only because the 14.0 code
        probed via ``getattr(record, "ids", None)`` — so the search-path
        refresh was never really exercised here. It is now.
        """
        return []

    def fields_get(self, fields: list[str], attributes: list[str]) -> dict[str, dict[str, Any]]:
        return {f: {"type": self._field_types.get(f, "char")} for f in fields}

    def sudo(self) -> FakeModel:
        return self

    def create(self, values: dict[str, Any]) -> FakeRecord:
        record = FakeRecord(self, values)
        self.env.created.append(record)
        return record

    def search(self, domain: list, **kwargs: Any) -> FakeRecordSet:
        return FakeRecordSet(self.name, [])

    def browse(self, record_id: int) -> FakeRecord:
        return FakeRecord(self, {"id": record_id})


class FakeCursor:
    def __init__(self) -> None:
        self.savepoints = 0
        self.rollbacks = 0

    @contextmanager
    def savepoint(self) -> Iterator[None]:
        self.savepoints += 1
        try:
            yield
        except Exception:
            self.rollbacks += 1
            raise


class FakeEnv:
    def __init__(self, user: Any = None, context: dict[str, Any] | None = None) -> None:
        self.context: dict[str, Any] = dict(context or {})
        self._refs: dict[str, FakeRecord] = {}
        self.cr = FakeCursor()
        self.created: list[FakeRecord] = []
        # Counts env-wide cache sweeps — the collateral damage we must not cause.
        self.cache_sweeps = 0
        # Flushing is env-level in 19 (`flush_all`), not per-record as in 14.
        self.flushed = 0
        self.uid = user

        base = FakeModel("res.company", self)
        currency = FakeRecord(FakeModel("res.currency", self), {"name": "IDR"})
        self.company = FakeRecord(base, {"name": "Test Co", "currency_id": currency})
        self.user = FakeRecord(FakeModel("res.users", self), {"login": "admin"})

    def __getitem__(self, model_name: str) -> FakeModel:
        return FakeModel(model_name, self)

    # -- cache plumbing, mirroring Odoo 19's Environment ----------------

    def flush_all(self) -> None:
        """Mirror ``odoo/orm/environments.py::flush_all`` (19.0).

        Env-wide, and unlike ``flush_recordset`` it has no empty-set escape —
        which is exactly why ``_refresh`` calls this one: ``search`` refreshes
        through an empty recordset, and ``flush_recordset`` would no-op there.
        Flushing writes pending values out; it never drops a cached value, so
        it causes no cache sweep.
        """
        self.flushed += 1

    def invalidate_all(self, flush: bool = True) -> None:
        """Mirror ``Environment.invalidate_all`` (19.0) — the env-wide sweep.

        Present so the tests can prove ``_refresh`` never calls it. Reaching
        for this is what re-reads unrelated records and turns a passing assert
        into an AccessError after an ``as_user`` step.
        """
        if flush:
            self.flush_all()
        for record in self.created:
            record._cache.clear()
        self.cache_sweeps += 1

    def ref(self, xml_id: str) -> FakeRecord:
        if xml_id not in self._refs:
            stub_model = FakeModel("res.partner", self)
            self._refs[xml_id] = FakeRecord(stub_model, {"xml_id": xml_id})
        return self._refs[xml_id]

    def __call__(self, **kwargs: Any) -> FakeEnv:
        """Return a NEW env recording user/context, as Odoo's env() does."""
        clone = object.__new__(FakeEnv)
        clone.__dict__.update(self.__dict__)
        if "user" in kwargs:
            clone.uid = kwargs["user"]
        if "context" in kwargs:
            clone.context = dict(kwargs["context"])
        return clone


class FakeFormX2Many:
    """Stands in for a Form's x2many proxy (``form.line_ids``)."""

    def __init__(self, lines: list[dict[str, Any]]) -> None:
        self._lines = lines

    @contextmanager
    def new(self) -> Iterator[Any]:
        line: dict[str, Any] = {}
        holder = _LineHolder(line)
        yield holder
        self._lines.append(line)

    @contextmanager
    def edit(self, index: int) -> Iterator[Any]:
        yield _LineHolder(self._lines[index])

    def remove(self, index: int) -> None:
        del self._lines[index]

    def __len__(self) -> int:
        return len(self._lines)


class _LineHolder:
    def __init__(self, store: dict[str, Any]) -> None:
        object.__setattr__(self, "_store", store)

    def __setattr__(self, name: str, value: Any) -> None:
        object.__getattribute__(self, "_store")[name] = value


class FakeForm:
    """Stand-in for ``odoo.tests.common.Form``, with one fake onchange.

    The onchange under test: setting ``partner_id`` clears ``type_id``.
    """

    def __init__(self, subject: Any) -> None:
        object.__setattr__(self, "_subject", subject)
        object.__setattr__(self, "_data", {"type_id": "preset", "order_line": []})
        object.__setattr__(self, "_x2m", {})

    def __setattr__(self, name: str, value: Any) -> None:
        data = object.__getattribute__(self, "_data")
        data[name] = value
        if name == "partner_id":
            data["type_id"] = False  # the "onchange"

    def __getattr__(self, name: str) -> Any:
        data = object.__getattribute__(self, "_data")
        if name == "order_line":
            x2m = object.__getattribute__(self, "_x2m")
            if "order_line" not in x2m:
                x2m["order_line"] = FakeFormX2Many(data["order_line"])
            return x2m["order_line"]
        if name in data:
            return data[name]
        raise AttributeError(name)

    def save(self) -> FakeRecord:
        subject = object.__getattribute__(self, "_subject")
        data = object.__getattribute__(self, "_data")
        model = subject if isinstance(subject, FakeModel) else subject._model
        return model.create(dict(data))


class FakeFormStrict:
    """Odoo-faithful stand-in for ``Form``: raises ``AssertionError`` — not
    ``AttributeError`` — for any attribute absent from the (fake) view.

    ``odoo.tests.common.Form.__getattr__`` does exactly this
    (``assert descr is not None, "%s was not found in the view" % field``).
    Regression guard for the bug where ``_read_path`` probed
    ``hasattr(current, "__len__")``: for a real Form, that probe raises
    ``AssertionError``, which ``hasattr()`` does not catch (it only swallows
    ``AttributeError``), so it used to blow up any ``asserts`` inside
    ``action: form``. ``FakeForm`` above raises the "normal" ``AttributeError``
    and never exposed this.
    """

    _view_fields: ClassVar[set[str]] = {"name", "partner_id", "type_id"}

    def __init__(self, subject: Any) -> None:
        object.__setattr__(self, "_subject", subject)
        object.__setattr__(self, "_data", {"type_id": "preset"})

    def __setattr__(self, name: str, value: Any) -> None:
        assert name in self._view_fields, f"{name} was not found in the view"
        data = object.__getattribute__(self, "_data")
        data[name] = value
        if name == "partner_id":
            data["type_id"] = False  # the "onchange"

    def __getattr__(self, name: str) -> Any:
        assert name in object.__getattribute__(
            self, "_view_fields"
        ), f"{name} was not found in the view"
        return object.__getattribute__(self, "_data").get(name)

    def save(self) -> FakeRecord:
        subject = object.__getattribute__(self, "_subject")
        data = object.__getattribute__(self, "_data")
        model = subject if isinstance(subject, FakeModel) else subject._model
        return model.create(dict(data))


# ----------------------------------------------------------------------
# Helper to build a YamlTransactionCase without unittest plumbing
# ----------------------------------------------------------------------


def _make_case(tmp_path: Path) -> YamlTransactionCase:
    """Build a YamlTransactionCase bypassing Odoo's TransactionCase."""
    case = object.__new__(YamlTransactionCase)
    case.registry = {}
    case.env = FakeEnv()  # type: ignore[attr-defined]

    # Fake subTest as a no-op context manager that still surfaces errors.
    class _NullSubTest:
        def __enter__(self) -> _NullSubTest:
            return self

        def __exit__(self, exc_type, exc, tb) -> bool:
            return False

    case.subTest = lambda **kwargs: _NullSubTest()  # type: ignore[attr-defined]
    return case


def _write_yaml(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "scenarios.yaml"
    path.write_text(content)
    return path


def _run(case: YamlTransactionCase, path: Path) -> None:
    with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
        case.run_yaml_scenario(str(path))


# ----------------------------------------------------------------------
# Existing behaviour (regression guards)
# ----------------------------------------------------------------------


class TestActionCreate:
    def test_creates_record_and_stores_in_registry(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "create one"
    steps:
      - step: "make partner"
        action: "create"
        model: "res.partner"
        save_as: "p1"
        values:
          name: "Acme"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert "p1" in case.registry
        assert case.registry["p1"]._values["name"] == "Acme"


class TestRefResolution:
    def test_ref_action_stores_record(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "ref"
    steps:
      - step: "resolve partner"
        action: "ref"
        xml_id: "base.res_partner_1"
        save_as: "p1"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert case.registry["p1"]._values["xml_id"] == "base.res_partner_1"

    def test_ref_prefix_in_values(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "ref prefix"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          partner_id: "REF: base.res_partner_1"
          name: "SO001"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert isinstance(case.registry["so"]._values["partner_id"], int)


class TestEvalIntegration:
    def test_eval_with_registry_access(self, tmp_path: Path) -> None:
        """The legacy EVAL: registry['x'].id spelling must keep working."""
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "eval registry"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          name: "SO001"
      - step: "make line"
        action: "create"
        model: "sale.order.line"
        save_as: "line"
        values:
          order_id: "EVAL: registry['so'].id"
          name: "Line 1"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert case.registry["line"]._values["order_id"] == case.registry["so"].id


class TestActionCall:
    def test_call_method_with_assert(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "call confirm"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          name: "SO001"
          state: "draft"
      - step: "confirm"
        action: "call"
        target: "so"
        method: "action_confirm"
        asserts:
          state:
            type: "value"
            expected: "sale"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert case.registry["so"]._values["state"] == "sale"


class TestScenarioIsolation:
    def test_registry_is_reset_between_scenarios(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "scenario A"
    steps:
      - step: "make a"
        action: "create"
        model: "res.partner"
        save_as: "shared_alias"
        values:
          name: "A"
  - name: "scenario B"
    steps:
      - step: "make b"
        action: "create"
        model: "res.partner"
        save_as: "different_alias"
        values:
          name: "B"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert "shared_alias" not in case.registry
        assert "different_alias" in case.registry


class TestUnknownAssertType:
    def test_raises_configuration_error(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "bad type"
    steps:
      - step: "make"
        action: "create"
        model: "res.partner"
        save_as: "p"
        values:
          name: "X"
      - step: "assert weird"
        action: "assert"
        target: "p"
        asserts:
          name:
            type: "telepathy"
            expected: "X"
""",
        )
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises(YamlStepError) as excinfo:
                case.run_yaml_scenario(str(path))
        assert "telepathy" in str(excinfo.value)


class TestDynamicValueResolution:
    def test_implicit_xml_id_only_for_relational(self, tmp_path: Path) -> None:
        # 'note' is a text field; a string with a dot must NOT be treated
        # as an xml_id (regression guard against false positives).
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "dot in text"
    steps:
      - step: "make"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          note: "version.1.0"
          name: "SO"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert case.registry["so"]._values["note"] == "version.1.0"


# ----------------------------------------------------------------------
# Error context (the 0.1.1 bugfix)
# ----------------------------------------------------------------------


class TestErrorContext:
    def test_unknown_action_yields_contextual_error(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "boom"
    steps:
      - step: "broken"
        action: "nonsense"
""",
        )
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises(YamlStepError) as excinfo:
                case.run_yaml_scenario(str(path))
        message = str(excinfo.value)
        assert "Scenario: 'boom'" in message
        assert "Step: 'broken'" in message

    def test_failed_assertion_keeps_lineage_and_gains_context(self, tmp_path: Path) -> None:
        """The 0.1.1 fix: an assertion failure must say WHERE it failed."""
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "bad assert"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          state: "draft"
      - step: "wrong assert"
        action: "assert"
        target: "so"
        asserts:
          state:
            type: "value"
            expected: "sale"
""",
        )
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises(YamlAssertionError) as excinfo:
                case.run_yaml_scenario(str(path))

        exc = excinfo.value
        # Still an AssertionError -> unittest reports FAIL, not ERROR.
        assert isinstance(exc, AssertionError)
        message = str(exc)
        assert "Scenario: 'bad assert'" in message
        assert "Step: 'wrong assert'" in message
        # ...and the original detail survives.
        assert "expected 'sale', got 'draft'" in message

    def test_context_is_not_applied_twice(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
setup:
  steps:
    - step: "broken setup"
      action: "nonsense"
scenarios:
  - name: "s"
    steps:
      - step: "never runs"
        action: "create"
        model: "res.partner"
        values: {name: "X"}
""",
        )
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises(YamlStepError) as excinfo:
                case.run_yaml_scenario(str(path))
        message = str(excinfo.value)
        assert message.count("Error in File:") == 1
        assert "Setup Step: 'broken setup'" in message


# ----------------------------------------------------------------------
# 0.2.0 — REC:, dotted asserts, as_user
# ----------------------------------------------------------------------


class TestRecPrefix:
    def test_bare_alias_yields_record_dot_id_yields_int(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "rec"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          name: "SO001"
      - step: "make line"
        action: "create"
        model: "sale.order.line"
        save_as: "line"
        values:
          order_id: "REC: so.id"
          name: "Line"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert case.registry["line"]._values["order_id"] == case.registry["so"].id

    def test_bare_alias_on_m2o_field_is_coerced_to_id(self, tmp_path: Path) -> None:
        """`REC: alias` yields the record; _coerce_for_field turns it into an id."""
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "rec bare"
    steps:
      - step: "make partner"
        action: "create"
        model: "res.partner"
        save_as: "p"
        values:
          name: "Acme"
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          partner_id: "REC: p"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert case.registry["so"]._values["partner_id"] == case.registry["p"].id

    def test_dotted_path_traverses_relations(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "rec dotted"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          name: "SO"
          note: "REC: company.currency_id.name"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert case.registry["so"]._values["note"] == "IDR"

    def test_builtin_company_and_user_aliases_are_seeded(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "builtins"
    steps:
      - step: "noop"
        action: "assert"
        target: "company"
        asserts:
          name:
            type: "value"
            expected: "Test Co"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert "company" in case.registry
        assert "user" in case.registry

    def test_save_as_overrides_builtin_alias(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "override"
    steps:
      - step: "make company"
        action: "create"
        model: "res.company"
        save_as: "company"
        values:
          name: "Mine"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert case.registry["company"]._values["name"] == "Mine"

    def test_unknown_alias_is_a_config_error(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "bad alias"
    steps:
      - step: "make"
        action: "create"
        model: "sale.order"
        values:
          partner_id: "REC: nope.id"
""",
        )
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises(YamlStepError) as excinfo:
                case.run_yaml_scenario(str(path))
        assert "nope" in str(excinfo.value)

    def test_rejects_calls_and_operators(self, tmp_path: Path) -> None:
        case = _make_case(tmp_path)
        case.registry["x"] = FakeRecord(FakeModel("res.partner", case.env), {})
        with pytest.raises(YamlConfigurationError):
            case._resolve_rec_path("x.foo()")
        with pytest.raises(YamlConfigurationError):
            case._resolve_rec_path("x.ids[0]")


class TestAssertExpectedPrefixes:
    def test_m2o_assert_against_registry_record(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "m2o vs record"
    steps:
      - step: "make partner"
        action: "create"
        model: "res.partner"
        save_as: "p"
        values:
          name: "Acme"
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          partner_id: "REC: p"
      - step: "assert m2o"
        action: "assert"
        target: "so"
        asserts:
          partner_id:
            type: "m2o"
            expected: "REC: p"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)  # must not raise

    def test_m2o_assert_failure_reports_ids(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "m2o mismatch"
    steps:
      - step: "make a"
        action: "create"
        model: "res.partner"
        save_as: "a"
        values: {name: "A"}
      - step: "make b"
        action: "create"
        model: "res.partner"
        save_as: "b"
        values: {name: "B"}
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          partner_id: "REC: a"
      - step: "assert wrong"
        action: "assert"
        target: "so"
        asserts:
          partner_id:
            type: "m2o"
            expected: "REC: b"
""",
        )
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises(YamlAssertionError):
                case.run_yaml_scenario(str(path))

    def test_plain_string_expected_is_not_treated_as_xml_id(self, tmp_path: Path) -> None:
        """A dotted literal in `expected` must stay a literal."""
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "literal expected"
    steps:
      - step: "make"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          note: "version.1.0"
      - step: "assert"
        action: "assert"
        target: "so"
        asserts:
          note:
            type: "value"
            expected: "version.1.0"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)  # must not raise


class TestDottedAssertPaths:
    def test_reads_across_a_relation(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "dotted"
    steps:
      - step: "assert nested"
        action: "assert"
        target: "company"
        asserts:
          currency_id.name:
            type: "value"
            expected: "IDR"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)  # must not raise

    def test_empty_relation_is_a_test_failure(self, tmp_path: Path) -> None:
        case = _make_case(tmp_path)
        record = FakeRecord(
            FakeModel("sale.order", case.env), {"partner_id": FakeRecordSet("res.partner", [])}
        )
        with pytest.raises(YamlAssertionError):
            case._read_path(record, "partner_id.name")


class TestAsUser:
    def test_as_user_accepts_a_registry_alias(self, tmp_path: Path) -> None:
        """The whole point: a user created in-scenario, with no xml_id."""
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "as_user alias"
    steps:
      - step: "make user"
        action: "create"
        model: "res.users"
        save_as: "approver"
        values:
          name: "Approver"
      - step: "make order as that user"
        action: "create"
        model: "sale.order"
        save_as: "so"
        as_user: "approver"
        values:
          name: "SO"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert case.registry["so"].env.uid == case.registry["approver"].id

    def test_as_user_still_accepts_an_xml_id(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "as_user xmlid"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        as_user: "base.user_admin"
        values:
          name: "SO"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert case.registry["so"].env.uid == case.env.ref("base.user_admin").id

    def test_as_user_applies_to_assert(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "as_user on assert"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          name: "SO"
      - step: "assert as admin"
        action: "assert"
        target: "so"
        as_user: "base.user_admin"
        asserts:
          name:
            type: "value"
            expected: "SO"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)  # must not raise


# ----------------------------------------------------------------------
# 0.3.0 — auto-refresh, expect_error, wizard
# ----------------------------------------------------------------------


class TestAutoRefresh:
    _YAML = """
options:
  auto_refresh: {auto_refresh}
scenarios:
  - name: "stale compute"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          state: "draft"
      - step: "confirm (leaves a stale cache)"
        action: "call"
        target: "so"
        method: "action_confirm_stale"
      - step: "assert new state"
        action: "assert"
        target: "so"
        asserts:
          state:
            type: "value"
            expected: "confirm"
"""

    def test_enabled_reads_fresh_values(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, self._YAML.format(auto_refresh="true"))
        case = _make_case(tmp_path)
        _run(case, path)  # must not raise: the cache was invalidated

    def test_default_is_off_so_the_stale_cache_is_read(self, tmp_path: Path) -> None:
        """The default must stay OFF — turning it on breaks existing suites.

        Enabling the refresh forces a re-read from the database, and a re-read
        runs record rules a cached value never had to pass. Against real Odoo
        that turned 15 of ssi_school's 79 passing tests into AccessError. So the
        library ships with the old (cache-reading) behaviour, and authors opt in
        per file when they are ready to fix what it uncovers.
        """
        no_options = self._YAML.replace("options:\n  auto_refresh: {auto_refresh}\n", "")
        path = _write_yaml(tmp_path, no_options)
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises(YamlAssertionError) as excinfo:
                case.run_yaml_scenario(str(path))
        assert "got 'draft'" in str(excinfo.value)

    def test_per_step_refresh_overrides_the_default(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "step override"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          state: "draft"
      - step: "confirm"
        action: "call"
        target: "so"
        method: "action_confirm_stale"
      - step: "assert fresh on purpose"
        action: "assert"
        target: "so"
        refresh: true
        asserts:
          state:
            type: "value"
            expected: "confirm"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)  # must not raise: we asked for the fresh value

    def test_class_attribute_enables_refresh(self, tmp_path: Path) -> None:
        no_options = self._YAML.replace("options:\n  auto_refresh: {auto_refresh}\n", "")
        path = _write_yaml(tmp_path, no_options)
        case = _make_case(tmp_path)
        case.auto_refresh = True
        _run(case, path)  # must not raise

    def test_refresh_is_scoped_to_the_asserted_record(self, tmp_path: Path) -> None:
        """Regression: auto-refresh must NOT sweep the whole env cache.

        `env.invalidate_all()` empties the cache for every record in the
        environment, forcing unrelated records to be re-read from the database.
        A re-read runs record rules the cached value never had to pass, so in
        real Odoo this turned passing asserts into AccessError whenever an
        earlier step had run `as_user`. Caught against ssi_school: 15 tests
        that pass on 0.1.0 errored on 0.4.0.

        On 19.0 `invalidate_recordset()` is scoped by construction, so this is
        no longer something `_refresh` has to get right — but the assertion
        stays: it now guards against anyone reaching for `env.invalidate_all()`
        as a convenient big hammer.
        """
        path = _write_yaml(
            tmp_path,
            """
options:
  auto_refresh: true
scenarios:
  - name: "scoped refresh"
    steps:
      - step: "make a neighbour record"
        action: "create"
        model: "res.partner"
        save_as: "neighbour"
        values:
          name: "Neighbour"
      - step: "make the subject"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          state: "draft"
      - step: "confirm (leaves a stale cache)"
        action: "call"
        target: "so"
        method: "action_confirm_stale"
      - step: "assert the subject"
        action: "assert"
        target: "so"
        asserts:
          state:
            type: "value"
            expected: "confirm"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        # The subject was refreshed (the assert passed), but no env-wide sweep
        # happened — the neighbour's cache was never touched.
        assert case.env.cache_sweeps == 0

    def test_refresh_on_search_flushes_through_an_empty_recordset(self, tmp_path: Path) -> None:
        """`search` refreshes through `env[model]`, which holds no ids.

        This path had no coverage at all before 19.0: `_action_search` was
        never executed by any test, and the 14.0 `_refresh` probed for `.ids`
        with a `getattr` default, so a fake lacking `ids` still sailed through.
        It matters here because 19's `flush_recordset()` opens with
        `if not self: return` — refreshing a search through it would flush
        nothing and silently stop the search seeing prior writes. `_refresh`
        therefore goes through `env.flush_all()`, and this proves the flush
        really happens while no cache is dropped (there is none to drop).
        """
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "search refreshes"
    steps:
      - step: "find orders"
        action: "search"
        model: "sale.order"
        domain: []
        save_as: "found"
        refresh: true
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)  # must not raise: env[model] has ids == []
        assert case.env.flushed == 1
        assert case.env.cache_sweeps == 0

    def test_manual_invalidate_recordset_step_works(self, tmp_path: Path) -> None:
        """A manual cache-drop step keeps working — in this series' spelling.

        On 14.0 this test drove `method: "invalidate_cache"`, the spelling the
        533 manual steps across the SSI corpus use. Odoo 17 removed that method,
        so on 19 such a step raises AttributeError rather than being harmless:
        scenarios carried over from a 14.0 module must rename it to
        `invalidate_recordset` — or better, drop the manual step and use
        `refresh: true`, which is what this library exists to do for them.
        """
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "manual cache drop"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          state: "draft"
      - step: "confirm"
        action: "call"
        target: "so"
        method: "action_confirm_stale"
      - step: "Invalidate cache after confirm"
        action: "call"
        target: "so"
        method: "invalidate_recordset"
      - step: "assert"
        action: "assert"
        target: "so"
        asserts:
          state:
            type: "value"
            expected: "confirm"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)  # must not raise


class TestExpectError:
    def test_expected_exception_passes_and_rolls_back(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "negative path"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          state: "draft"
      - step: "confirm must fail"
        action: "call"
        target: "so"
        method: "action_boom"
        expect_error:
          type: "ValueError"
          message_contains: "no lines"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)  # must not raise
        # The savepoint saw the error and rolled back.
        assert case.env.cr.savepoints == 1
        assert case.env.cr.rollbacks == 1

    def test_no_exception_raised_is_a_failure(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "should have failed"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values:
          state: "draft"
      - step: "confirm"
        action: "call"
        target: "so"
        method: "action_confirm"
        expect_error:
          type: "ValueError"
""",
        )
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises(YamlAssertionError) as excinfo:
                case.run_yaml_scenario(str(path))
        assert "nothing was raised" in str(excinfo.value)

    def test_message_mismatch_is_a_failure(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "wrong message"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values: {state: "draft"}
      - step: "boom"
        action: "call"
        target: "so"
        method: "action_boom"
        expect_error:
          type: "ValueError"
          message_contains: "totally different"
""",
        )
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises(YamlAssertionError):
                case.run_yaml_scenario(str(path))

    def test_unknown_error_type_is_a_config_error(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "bogus type"
    steps:
      - step: "make order"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values: {state: "draft"}
      - step: "boom"
        action: "call"
        target: "so"
        method: "action_boom"
        expect_error:
          type: "NotARealError"
""",
        )
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises(YamlStepError) as excinfo:
                case.run_yaml_scenario(str(path))
        assert "NotARealError" in str(excinfo.value)

    def test_expect_error_with_save_as_is_rejected(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "contradiction"
    steps:
      - step: "make"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values: {name: "X"}
        expect_error:
          type: "ValueError"
""",
        )
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises(YamlStepError) as excinfo:
                case.run_yaml_scenario(str(path))
        assert "save_as" in str(excinfo.value)


class TestActionUnlink:
    def test_unlink_marks_record(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "delete"
    steps:
      - step: "make"
        action: "create"
        model: "res.partner"
        save_as: "p"
        values: {name: "X"}
      - step: "delete it"
        action: "unlink"
        target: "p"
        context:
          force_unlink: true
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert case.registry["p"]._values["unlinked"] is True


class TestActionWizard:
    def test_wizard_gets_active_context_and_calls_method(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "cancel via wizard"
    steps:
      - step: "make doc"
        action: "create"
        model: "account.amortization"
        save_as: "doc"
        values: {state: "open"}
      - step: "make reason"
        action: "create"
        model: "base.cancel_reason"
        save_as: "reason"
        values: {name: "Typo"}
      - step: "cancel it"
        action: "wizard"
        model: "base.select_cancel_reason"
        target: "doc"
        save_as: "wiz"
        values:
          cancel_reason_id: "REC: reason"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)

        wizard = case.registry["wiz"]
        ctx = wizard.env.context
        assert ctx["active_model"] == "account.amortization"
        assert ctx["active_id"] == case.registry["doc"].id
        assert ctx["active_ids"] == [case.registry["doc"].id]
        # cancel_reason_id is m2o -> coerced to an id
        assert wizard._values["cancel_reason_id"] == case.registry["reason"].id

    def test_explicit_context_wins_over_wizard_defaults(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "ctx override"
    steps:
      - step: "make doc"
        action: "create"
        model: "sale.order"
        save_as: "doc"
        values: {name: "SO"}
      - step: "wizard"
        action: "wizard"
        model: "some.wizard"
        target: "doc"
        save_as: "wiz"
        context:
          active_model: "custom.model"
        values: {}
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert case.registry["wiz"].env.context["active_model"] == "custom.model"


class TestActionWizardMultiRecord:
    """``action: wizard`` over a target holding more than one record.

    The recordsets here are seeded straight into ``case.registry`` and
    ``_action_wizard`` is called directly, rather than going through ``_run``:
    ``run_yaml_scenario`` opens with ``_reset_registry()``, which would drop any
    alias seeded beforehand. ``FakeModel.search`` always yields an *empty*
    ``FakeRecordSet``, so an ``action: search`` step cannot produce a
    multi-record target either.
    """

    @staticmethod
    def _seed(case: YamlTransactionCase, model_name: str, count: int) -> list[FakeRecord]:
        """Bind a ``count``-record recordset to the ``docs`` alias."""
        model = FakeModel(model_name, case.env)
        records = [FakeRecord(model, {"state": "open"}) for _ in range(count)]
        case.registry["docs"] = FakeRecordSet(model_name, records)
        return records

    def test_multi_record_target_fills_active_ids_and_first_active_id(self, tmp_path: Path) -> None:
        """Three selected rows reach the wizard the way the list view sends them."""
        case = _make_case(tmp_path)
        records = self._seed(case, "account.amortization", 3)

        case._action_wizard(
            {
                "action": "wizard",
                "model": "base.select_cancel_reason",
                "target": "docs",
                "save_as": "wiz",
                "values": {},
            }
        )

        ctx = case.registry["wiz"].env.context
        assert ctx["active_model"] == "account.amortization"
        assert ctx["active_ids"] == [rec.id for rec in records]
        assert ctx["active_id"] == records[0].id

    def test_empty_target_is_legal_and_yields_false_active_id(self, tmp_path: Path) -> None:
        """An empty selection still opens the wizard instead of blowing up."""
        case = _make_case(tmp_path)
        self._seed(case, "account.amortization", 0)

        case._action_wizard(
            {
                "action": "wizard",
                "model": "base.select_cancel_reason",
                "target": "docs",
                "save_as": "wiz",
                "values": {},
            }
        )

        ctx = case.registry["wiz"].env.context
        assert ctx["active_id"] is False
        assert ctx["active_ids"] == []

    def test_explicit_context_still_wins_for_a_multi_record_target(self, tmp_path: Path) -> None:
        """``context:`` overrides the wizard defaults regardless of target length."""
        case = _make_case(tmp_path)
        records = self._seed(case, "sale.order", 3)

        case._action_wizard(
            {
                "action": "wizard",
                "model": "some.wizard",
                "target": "docs",
                "save_as": "wiz",
                "context": {"active_model": "custom.model"},
                "values": {},
            }
        )

        ctx = case.registry["wiz"].env.context
        assert ctx["active_model"] == "custom.model"
        assert ctx["active_ids"] == [rec.id for rec in records]

    def test_fake_recordset_id_mirrors_ensure_one(self, tmp_path: Path) -> None:
        """Guard the guard: without this, the multi-record test cannot fail.

        ``FakeRecordSet.id`` is the only thing that makes a regression back to
        ``target.id`` surface as Odoo's singleton error rather than passing
        silently.
        """
        case = _make_case(tmp_path)
        model = FakeModel("res.partner", case.env)

        assert FakeRecordSet("res.partner", []).id is False

        single = FakeRecord(model, {})
        assert FakeRecordSet("res.partner", [single]).id == single.id

        pair = FakeRecordSet("res.partner", [FakeRecord(model, {}), FakeRecord(model, {})])
        with pytest.raises(ValueError, match="Expected singleton"):
            _ = pair.id


# ----------------------------------------------------------------------
# 0.4.0 — form action, shared setup
# ----------------------------------------------------------------------


class TestActionForm:
    def test_onchange_fires_and_asserts_run_pre_save(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "onchange"
    steps:
      - step: "make partner"
        action: "create"
        model: "res.partner"
        save_as: "p"
        values: {name: "Acme"}
      - step: "form clears type on partner change"
        action: "form"
        model: "sale.order"
        save_as: "so"
        values:
          name: "SO"
          partner_id: "REC: p"
        asserts:
          type_id:
            type: "value"
            operator: "is_falsy"
""",
        )
        case = _make_case(tmp_path)
        case.form_class = FakeForm
        _run(case, path)
        # save() ran and the record landed in the registry.
        assert case.registry["so"]._values["name"] == "SO"

    def test_ops_allow_repeated_assignment(self, tmp_path: Path) -> None:
        """Setting the same field twice must be expressible — a mapping can't."""
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "repeat set"
    steps:
      - step: "make partner"
        action: "create"
        model: "res.partner"
        save_as: "p"
        values: {name: "Acme"}
      - step: "drive the form"
        action: "form"
        model: "sale.order"
        save: false
        ops:
          - set: {partner_id: "REC: p"}
          - set: {type_id: "restored"}
          - set: {partner_id: "REC: p"}
          - assert:
              type_id:
                type: "value"
                operator: "is_falsy"
""",
        )
        case = _make_case(tmp_path)
        case.form_class = FakeForm
        _run(case, path)  # must not raise

    def test_x2m_new_and_remove(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "x2m"
    steps:
      - step: "make product"
        action: "create"
        model: "product.product"
        save_as: "prod"
        values: {name: "Widget"}
      - step: "build order"
        action: "form"
        model: "sale.order"
        save_as: "so"
        ops:
          - set: {name: "SO"}
          - new:
              field: "order_line"
              values:
                product_id: "REC: prod"
                product_uom_qty: 2
          - new:
              field: "order_line"
              values:
                product_id: "REC: prod"
                product_uom_qty: 5
          - remove: {field: "order_line", index: 0}
""",
        )
        case = _make_case(tmp_path)
        case.form_class = FakeForm
        _run(case, path)
        lines = case.registry["so"]._values["order_line"]
        assert len(lines) == 1
        assert lines[0]["product_uom_qty"] == 5

    def test_form_values_pass_records_not_ids(self, tmp_path: Path) -> None:
        """Form assignment needs the RECORD; no _coerce_for_field here."""
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "record not id"
    steps:
      - step: "make partner"
        action: "create"
        model: "res.partner"
        save_as: "p"
        values: {name: "Acme"}
      - step: "form"
        action: "form"
        model: "sale.order"
        save_as: "so"
        values:
          partner_id: "REC: p"
""",
        )
        case = _make_case(tmp_path)
        case.form_class = FakeForm
        _run(case, path)
        assert case.registry["so"]._values["partner_id"] is case.registry["p"]

    def test_asserts_survive_form_getattr_raising_assertionerror(self, tmp_path: Path) -> None:
        """Regression: a real Odoo Form raises AssertionError (not
        AttributeError) for any name missing from the view, including dunder
        probes like __len__. _read_path must not detect an "empty container"
        via hasattr(current, "__len__"), because hasattr() only swallows
        AttributeError and would let this AssertionError escape uncaught,
        breaking asserts inside every action: form step regardless of which
        field is being asserted.
        """
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "onchange, Form raises AssertionError for unknown names"
    steps:
      - step: "make partner"
        action: "create"
        model: "res.partner"
        save_as: "p"
        values: {name: "Acme"}
      - step: "form clears type on partner change"
        action: "form"
        model: "sale.order"
        save: false
        values:
          partner_id: "REC: p"
        asserts:
          type_id:
            type: "value"
            operator: "is_falsy"
""",
        )
        case = _make_case(tmp_path)
        case.form_class = FakeFormStrict
        _run(case, path)

    def test_model_and_target_are_mutually_exclusive(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "both"
    steps:
      - step: "make"
        action: "create"
        model: "sale.order"
        save_as: "so"
        values: {name: "SO"}
      - step: "bad form"
        action: "form"
        model: "sale.order"
        target: "so"
""",
        )
        case = _make_case(tmp_path)
        case.form_class = FakeForm
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises(YamlStepError):
                case.run_yaml_scenario(str(path))


class TestSharedSetup:
    def test_setup_runs_before_every_scenario_without_leaking(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
setup:
  steps:
    - step: "shared journal"
      action: "create"
      model: "account.journal"
      save_as: "journal"
      values:
        name: "Shared"

scenarios:
  - name: "A"
    steps:
      - step: "use journal"
        action: "assert"
        target: "journal"
        asserts:
          name:
            type: "value"
            expected: "Shared"
      - step: "make local"
        action: "create"
        model: "res.partner"
        save_as: "only_in_a"
        values: {name: "A"}

  - name: "B"
    steps:
      - step: "journal is here too"
        action: "assert"
        target: "journal"
        asserts:
          name:
            type: "value"
            expected: "Shared"
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        # Setup ran twice (once per scenario) — fresh records each time.
        journals = [r for r in case.env.created if r._name == "account.journal"]
        assert len(journals) == 2
        # Scenario A's own alias did not leak into B.
        assert "only_in_a" not in case.registry

    def test_skip_setup(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
setup:
  steps:
    - step: "shared"
      action: "create"
      model: "account.journal"
      save_as: "journal"
      values: {name: "Shared"}

scenarios:
  - name: "no setup"
    skip_setup: true
    steps:
      - step: "make"
        action: "create"
        model: "res.partner"
        save_as: "p"
        values: {name: "X"}
""",
        )
        case = _make_case(tmp_path)
        _run(case, path)
        assert "journal" not in case.registry


# ----------------------------------------------------------------------
# fake_models:
# ----------------------------------------------------------------------


class FakeRegistry:
    """Stands in for ``odoo.registry``, recording the calls the seam makes."""

    def __init__(self) -> None:
        self.models: dict[str, Any] = {"base": object()}
        self.calls: list[str] = []
        self.init_args: list[Any] = []

    def __contains__(self, name: str) -> bool:
        return name in self.models

    def __delitem__(self, name: str) -> None:
        self.calls.append(f"del:{name}")
        del self.models[name]

    def _setup_models__(self, cr: Any, model_names: Any) -> None:
        self.calls.append("setup")

    def init_models(self, cr: Any, names: Any, context: dict[str, Any]) -> None:
        self.calls.append("init")
        self.init_args.append((list(names), dict(context)))


class FakeAclEnv(FakeEnv):
    """A FakeEnv that owns a registry and answers ir.model lookups."""

    def __init__(self) -> None:
        super().__init__()
        self.registry = FakeRegistry()
        self.acl_rows: list[dict[str, Any]] = []
        self.invalidate_calls: list[int] = []
        self.invalidate_flush_args: list[bool] = []

    def __getitem__(self, model_name: str) -> Any:
        if model_name == "ir.model":
            return _FakeIrModel(self)
        if model_name == "ir.model.access":
            return _FakeIrModelAccess(self)
        return FakeModel(model_name, self)

    def invalidate_all(self, flush: bool = True) -> None:
        # Record WHEN this ran relative to registry deletions, and with which
        # flush setting, so the ordering contract in _unload_fake_models can be
        # asserted.
        self.invalidate_calls.append(len(self.registry.calls))
        self.invalidate_flush_args.append(flush)
        super().invalidate_all(flush=flush)


class _FakeIrModel:
    def __init__(self, env: FakeAclEnv) -> None:
        self.env = env

    def sudo(self) -> _FakeIrModel:
        return self

    def search(self, domain: list, **kwargs: Any) -> Any:
        model_name = domain[0][2]
        if model_name in self.env.registry.models:
            return FakeRecord(FakeModel("ir.model", self.env), {"model": model_name})
        return FakeRecordSet("ir.model", [])


class _FakeIrModelAccess:
    def __init__(self, env: FakeAclEnv) -> None:
        self.env = env

    def sudo(self) -> _FakeIrModelAccess:
        return self

    def create(self, values: dict[str, Any]) -> Any:
        self.env.acl_rows.append(values)
        return FakeRecord(FakeModel("ir.model.access", self.env), values)


class FakeMixinClass:
    """Stands in for an imported Odoo model class."""

    _name = "fake.mixin"
    _module = "probe_addon"


class FakeConsumerClass:
    _name = "fake.consumer"
    _module = "probe_addon"


def _make_fake_model_case(tmp_path: Path) -> YamlTransactionCase:
    case = _make_case(tmp_path)
    case.env = FakeAclEnv()  # type: ignore[attr-defined]
    case.cleanups = []  # type: ignore[attr-defined]
    case.addCleanup = lambda fn, *a, **kw: case.cleanups.append((fn, a, kw))  # type: ignore[attr-defined]
    case.__class__ = type(
        "ProbeCase", (YamlTransactionCase,), {"__module__": "odoo.addons.probe_addon.tests.test_x"}
    )
    return case


@contextmanager
def _patched_odoo_api(module_to_models: dict[str, list[Any]] | None = None) -> Iterator[list[Any]]:
    """Patch the two lazy Odoo lookups the seam performs."""
    added: list[Any] = []
    mapping = module_to_models if module_to_models is not None else {}

    def _fake_add_to_registry(registry: Any, klass: Any) -> Any:
        registry.models[klass._name] = klass
        registry.calls.append(f"add:{klass._name}")
        added.append(klass)
        return klass

    with mock.patch.object(case_module, "_add_to_registry", return_value=_fake_add_to_registry):
        with mock.patch.object(case_module, "_module_to_models", return_value=mapping):
            yield added


FAKE_MODELS_YAML = """
fake_models:
  classes:
    - "tests.test_case_smoke:FakeMixinClass"
    - "tests.test_case_smoke:FakeConsumerClass"

scenarios:
  - name: "trivial"
    steps:
      - step: "make"
        action: "create"
        model: "res.partner"
        save_as: "p"
        values: {name: "X"}
"""


class TestFakeModelsNonBreaking:
    """The new key must be inert for every file that does not use it."""

    def test_absent_key_never_touches_the_machinery(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            """
scenarios:
  - name: "plain"
    steps:
      - step: "make"
        action: "create"
        model: "res.partner"
        save_as: "p"
        values: {name: "X"}
""",
        )
        case = _make_fake_model_case(tmp_path)
        with mock.patch.object(
            case, "_setup_fake_models", side_effect=AssertionError("must not be called")
        ):
            _run(case, path)
        assert case.env.registry.calls == []


class TestFakeModelsLoading:
    def test_loads_classes_and_creates_tables(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, FAKE_MODELS_YAML)
        case = _make_fake_model_case(tmp_path)
        with _patched_odoo_api() as added:
            _run(case, path)
        assert [k._name for k in added] == ["fake.mixin", "fake.consumer"]
        assert case.env.registry.calls == [
            "add:fake.mixin",
            "add:fake.consumer",
            "setup",
            "init",
        ]

    def test_init_models_receives_names_and_addon(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, FAKE_MODELS_YAML)
        case = _make_fake_model_case(tmp_path)
        with _patched_odoo_api():
            _run(case, path)
        names, context = case.env.registry.init_args[0]
        assert names == ["fake.mixin", "fake.consumer"]
        # Addon derived from the test class's __module__, not guessed.
        assert context == {"module": "probe_addon"}

    def test_explicit_addon_key_wins(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            FAKE_MODELS_YAML.replace(
                '    - "tests.test_case_smoke:FakeConsumerClass"',
                '    - "tests.test_case_smoke:FakeConsumerClass"\n  addon: "other_addon"',
            ),
        )
        case = _make_fake_model_case(tmp_path)
        with _patched_odoo_api():
            _run(case, path)
        assert case.env.registry.init_args[0][1] == {"module": "other_addon"}

    def test_undeterminable_addon_raises(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, FAKE_MODELS_YAML)
        case = _make_fake_model_case(tmp_path)
        case.__class__ = type("Outside", (YamlTransactionCase,), {"__module__": "somewhere.else"})
        with _patched_odoo_api(), pytest.raises(YamlConfigurationError, match="owning addon"):
            _run(case, path)

    def test_teardown_registered_before_loading(self, tmp_path: Path) -> None:
        """A load that dies halfway must still unwind the registry."""
        path = _write_yaml(tmp_path, FAKE_MODELS_YAML)
        case = _make_fake_model_case(tmp_path)
        with mock.patch.object(
            case, "_load_fake_models", side_effect=RuntimeError("boom")
        ), pytest.raises(Exception, match="boom"):
            _run(case, path)
        assert [fn for fn, _a, _k in case.cleanups] == [case._unload_fake_models]

    def test_second_file_in_one_test_method_raises(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, FAKE_MODELS_YAML)
        case = _make_fake_model_case(tmp_path)
        with _patched_odoo_api():
            _run(case, path)
            with pytest.raises(YamlConfigurationError, match="already loaded fake models"):
                _run(case, path)


class TestFakeModelsUnloading:
    def _load(self, tmp_path: Path, mapping: dict[str, list[Any]]) -> YamlTransactionCase:
        path = _write_yaml(tmp_path, FAKE_MODELS_YAML)
        case = _make_fake_model_case(tmp_path)
        with _patched_odoo_api(mapping):
            _run(case, path)
        return case

    @staticmethod
    def _run_cleanup(case: YamlTransactionCase, mapping: dict[str, list[Any]]) -> None:
        """Invoke the registered teardown with the Odoo lookups still patched."""
        fn, args, kwargs = case.cleanups[0]
        with mock.patch.object(case_module, "_module_to_models", return_value=mapping):
            fn(*args, **kwargs)

    def test_invalidate_runs_before_any_deletion(self, tmp_path: Path) -> None:
        """The ordering contract that a KeyError taught us.

        ``_setup_models__`` internally calls ``env.invalidate_all() ->
        flush_all()``, which looks every pending model up in the registry.
        Deleting first leaves the env holding a name the registry no longer
        has. Against real Odoo 19 that is a KeyError, not a soft failure.
        """
        case = self._load(tmp_path, {})
        case.env.registry.calls.clear()
        case.env.invalidate_calls.clear()

        self._run_cleanup(case, {})

        assert case.env.invalidate_calls == [0], "invalidate_all must precede every del"
        assert case.env.registry.calls == ["del:fake.consumer", "del:fake.mixin", "setup"]

    def test_invalidate_does_not_flush(self, tmp_path: Path) -> None:
        """Flushing needs a healthy cursor; teardown must not require one.

        It would also be wasted work: the values would be written to a table
        that is about to be dropped from the registry.
        """
        case = self._load(tmp_path, {})
        case.env.invalidate_flush_args.clear()
        self._run_cleanup(case, {})
        assert case.env.invalidate_flush_args == [False]

    def test_registry_is_cleaned_even_if_resetup_fails(self, tmp_path: Path) -> None:
        """A poisoned cursor must not leak the fake model into later tests.

        Registry mutation is process-wide, not transactional. If a scenario
        aborts the transaction, every statement afterwards raises — including
        the closing re-setup. Giving up there would leave the fake model in the
        registry for the rest of the run.
        """
        case = self._load(tmp_path, {})
        case.env.registry._setup_models__ = mock.Mock(  # type: ignore[method-assign]
            side_effect=RuntimeError("current transaction is aborted")
        )
        self._run_cleanup(case, {})
        assert set(case.env.registry.models) == {"base"}
        assert case._fake_model_classes is None

    def test_only_models_added_by_us_are_removed(self, tmp_path: Path) -> None:
        case = self._load(tmp_path, {})
        self._run_cleanup(case, {})
        assert set(case.env.registry.models) == {"base"}

    def test_module_to_models_is_cleaned(self, tmp_path: Path) -> None:
        """Importing the classes registered them; leaving them resurrects them."""
        mapping: dict[str, list[Any]] = {"probe_addon": [FakeMixinClass, FakeConsumerClass]}
        case = self._load(tmp_path, mapping)
        self._run_cleanup(case, mapping)
        assert mapping["probe_addon"] == []

    def test_unload_clears_the_loaded_marker(self, tmp_path: Path) -> None:
        case = self._load(tmp_path, {})
        assert case._fake_model_classes is not None
        self._run_cleanup(case, {})
        assert case._fake_model_classes is None


class TestFakeModelsAcl:
    def test_rows_created_for_each_model_and_group(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, FAKE_MODELS_YAML)
        case = _make_fake_model_case(tmp_path)
        with _patched_odoo_api():
            _run(case, path)
        assert len(case.env.acl_rows) == 2
        for row in case.env.acl_rows:
            assert row["perm_read"] and row["perm_write"]
            assert row["perm_create"] and row["perm_unlink"]

    def test_acl_false_creates_nothing(self, tmp_path: Path) -> None:
        path = _write_yaml(
            tmp_path,
            FAKE_MODELS_YAML.replace(
                '    - "tests.test_case_smoke:FakeConsumerClass"',
                '    - "tests.test_case_smoke:FakeConsumerClass"\n  acl: false',
            ),
        )
        case = _make_fake_model_case(tmp_path)
        with _patched_odoo_api():
            _run(case, path)
        assert case.env.acl_rows == []

    def test_missing_ir_model_row_is_reported(self, tmp_path: Path) -> None:
        path = _write_yaml(tmp_path, FAKE_MODELS_YAML)
        case = _make_fake_model_case(tmp_path)

        def _add_without_registering(registry: Any, klass: Any) -> Any:
            registry.calls.append(f"add:{klass._name}")
            return klass  # never lands in registry.models -> no ir.model row

        with mock.patch.object(
            case_module, "_add_to_registry", return_value=_add_without_registering
        ), mock.patch.object(case_module, "_module_to_models", return_value={}):
            with pytest.raises(Exception, match="has no ir.model row"):
                _run(case, path)


class TestFakeModelsErrors:
    def _run_with(self, tmp_path: Path, ref: str) -> None:
        path = _write_yaml(
            tmp_path,
            FAKE_MODELS_YAML.replace(
                '    - "tests.test_case_smoke:FakeMixinClass"\n'
                '    - "tests.test_case_smoke:FakeConsumerClass"',
                f'    - "{ref}"',
            ),
        )
        case = _make_fake_model_case(tmp_path)
        with _patched_odoo_api():
            _run(case, path)

    def test_unimportable_module(self, tmp_path: Path) -> None:
        with pytest.raises(YamlConfigurationError, match="cannot import module"):
            self._run_with(tmp_path, "no.such.module:Thing")

    def test_missing_attribute_lists_public_names(self, tmp_path: Path) -> None:
        with pytest.raises(YamlConfigurationError, match="has no attribute 'Nope'"):
            self._run_with(tmp_path, "tests.test_case_smoke:Nope")

    def test_not_a_model_class(self, tmp_path: Path) -> None:
        with pytest.raises(YamlConfigurationError, match="not an Odoo model class"):
            self._run_with(tmp_path, "tests.test_case_smoke:FakeRegistry")

    def test_already_real_model_is_refused(self, tmp_path: Path) -> None:
        """The fake module leaked into addon load: the model is real now."""
        path = _write_yaml(tmp_path, FAKE_MODELS_YAML)
        case = _make_fake_model_case(tmp_path)
        case.env.registry.models["fake.mixin"] = object()
        with _patched_odoo_api(), pytest.raises(YamlConfigurationError, match="already exist"):
            _run(case, path)
