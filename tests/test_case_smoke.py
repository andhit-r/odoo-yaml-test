"""Smoke tests for case.py without a live Odoo install.

We construct a fake ``env``/``model``/``record`` graph that mimics the
public ORM API surface used by the YamlTransactionCase. This lets us
verify dispatch logic, error wrapping, scenario isolation, and dynamic
value resolution end-to-end without needing a Postgres database.

These tests instantiate the YamlTransactionCase placeholder directly
through ``object.__new__`` to bypass the unittest setUp machinery,
because we are not actually running an Odoo TransactionCase.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from odoo_yaml_test.case import YamlTransactionCase
from odoo_yaml_test.exceptions import (
    YamlAssertionError,
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
        self.env = model.env

    def __getattr__(self, name: str) -> Any:
        if name in self._values:
            return self._values[name]
        raise AttributeError(name)

    @property
    def ids(self) -> list[int]:
        return [self.id]

    def __len__(self) -> int:
        return 1

    def __iter__(self):
        yield self

    def write(self, values: dict[str, Any]) -> bool:
        self._values.update(values)
        return True

    def with_env(self, env: FakeEnv) -> FakeRecord:
        return self

    # methods used by the action_call test
    def action_confirm(self) -> bool:
        self._values["state"] = "sale"
        return True


class FakeModel:
    def __init__(self, name: str, env: FakeEnv) -> None:
        self.name = name
        self.env = env
        self._field_types: dict[str, str] = {
            "partner_id": "many2one",
            "product_id": "many2one",
            "order_id": "many2one",
            "tag_ids": "many2many",
            "order_line": "one2many",
            "name": "char",
            "note": "text",
            "state": "char",
            "date_order": "datetime",
            "product_uom_qty": "float",
        }

    def fields_get(self, fields: list[str], attributes: list[str]) -> dict[str, dict[str, Any]]:
        return {f: {"type": self._field_types.get(f, "char")} for f in fields}

    def create(self, values: dict[str, Any]) -> FakeRecord:
        return FakeRecord(self, values)

    def search(self, domain: list, **kwargs: Any) -> list[FakeRecord]:
        return []


class FakeEnv:
    def __init__(self) -> None:
        self.context: dict[str, Any] = {}
        self._refs: dict[str, FakeRecord] = {}

    def __getitem__(self, model_name: str) -> FakeModel:
        return FakeModel(model_name, self)

    def ref(self, xml_id: str) -> FakeRecord:
        if xml_id not in self._refs:
            stub_model = FakeModel("res.partner", self)
            self._refs[xml_id] = FakeRecord(stub_model, {"xml_id": xml_id})
        return self._refs[xml_id]

    def __call__(self, **kwargs: Any) -> FakeEnv:
        return self


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


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


class TestActionCreate:
    def test_creates_record_and_stores_in_registry(self, tmp_path: Path) -> None:
        yaml_content = """
scenarios:
  - name: "create one"
    steps:
      - step: "make partner"
        action: "create"
        model: "res.partner"
        save_as: "p1"
        values:
          name: "Acme"
"""
        path = _write_yaml(tmp_path, yaml_content)
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            case.run_yaml_scenario(str(path))
        assert "p1" in case.registry
        assert case.registry["p1"]._values["name"] == "Acme"


class TestRefResolution:
    def test_ref_action_stores_record(self, tmp_path: Path) -> None:
        yaml_content = """
scenarios:
  - name: "ref"
    steps:
      - step: "resolve partner"
        action: "ref"
        xml_id: "base.res_partner_1"
        save_as: "p1"
"""
        path = _write_yaml(tmp_path, yaml_content)
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            case.run_yaml_scenario(str(path))
        assert case.registry["p1"]._values["xml_id"] == "base.res_partner_1"

    def test_ref_prefix_in_values(self, tmp_path: Path) -> None:
        yaml_content = """
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
"""
        path = _write_yaml(tmp_path, yaml_content)
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            case.run_yaml_scenario(str(path))
        # partner_id should be resolved to an integer id
        assert isinstance(case.registry["so"]._values["partner_id"], int)


class TestEvalIntegration:
    def test_eval_with_registry_access(self, tmp_path: Path) -> None:
        yaml_content = """
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
"""
        path = _write_yaml(tmp_path, yaml_content)
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            case.run_yaml_scenario(str(path))
        so_id = case.registry["so"].id
        assert case.registry["line"]._values["order_id"] == so_id


class TestActionCall:
    def test_call_method_with_assert(self, tmp_path: Path) -> None:
        yaml_content = """
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
"""
        path = _write_yaml(tmp_path, yaml_content)
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            case.run_yaml_scenario(str(path))
        assert case.registry["so"]._values["state"] == "sale"


class TestAssertionFailures:
    def test_failed_value_assertion_raises_assertion_error(self, tmp_path: Path) -> None:
        yaml_content = """
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
"""
        path = _write_yaml(tmp_path, yaml_content)
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises((YamlAssertionError, AssertionError)):
                case.run_yaml_scenario(str(path))


class TestScenarioIsolation:
    def test_registry_is_reset_between_scenarios(self, tmp_path: Path) -> None:
        yaml_content = """
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
"""
        path = _write_yaml(tmp_path, yaml_content)
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            case.run_yaml_scenario(str(path))
        # After scenario B, only B's alias remains; A's was wiped.
        assert "shared_alias" not in case.registry
        assert "different_alias" in case.registry


class TestErrorContext:
    def test_unknown_action_yields_contextual_error(self, tmp_path: Path) -> None:
        yaml_content = """
scenarios:
  - name: "boom"
    steps:
      - step: "broken"
        action: "nonsense"
"""
        path = _write_yaml(tmp_path, yaml_content)
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises(YamlStepError) as excinfo:
                case.run_yaml_scenario(str(path))
        message = str(excinfo.value)
        assert "Scenario: 'boom'" in message
        assert "Step: 'broken'" in message
        assert "nonsense" in message


class TestUnknownAssertType:
    def test_raises_configuration_error(self, tmp_path: Path) -> None:
        yaml_content = """
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
"""
        path = _write_yaml(tmp_path, yaml_content)
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            with pytest.raises(YamlStepError) as excinfo:
                case.run_yaml_scenario(str(path))
        assert "telepathy" in str(excinfo.value)


class TestDynamicValueResolution:
    def test_implicit_xml_id_only_for_relational(self, tmp_path: Path) -> None:
        # 'note' is a text field; a string with a dot must NOT be treated
        # as an xml_id (regression guard against false positives).
        yaml_content = """
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
"""
        path = _write_yaml(tmp_path, yaml_content)
        case = _make_case(tmp_path)
        with mock.patch.object(case, "_resolve_yaml_path", return_value=path):
            case.run_yaml_scenario(str(path))
        assert case.registry["so"]._values["note"] == "version.1.0"
