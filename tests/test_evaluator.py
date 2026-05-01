"""Tests for the safe_eval evaluator."""

from __future__ import annotations

from datetime import date, datetime

import pytest

from odoo_yaml_test.evaluator import safe_eval
from odoo_yaml_test.exceptions import YamlConfigurationError


class TestLiteralFastPath:
    def test_integer(self) -> None:
        assert safe_eval("42") == 42

    def test_string(self) -> None:
        assert safe_eval("'hello'") == "hello"

    def test_list(self) -> None:
        assert safe_eval("[1, 2, 3]") == [1, 2, 3]

    def test_dict(self) -> None:
        assert safe_eval("{'a': 1}") == {"a": 1}


class TestArithmetic:
    def test_addition(self) -> None:
        assert safe_eval("1 + 2") == 3

    def test_arithmetic_with_locals(self) -> None:
        assert safe_eval("x * 2", {"x": 21}) == 42


class TestWhitelistedNames:
    def test_datetime_now(self) -> None:
        result = safe_eval("datetime.now()")
        assert isinstance(result, datetime)

    def test_date_today(self) -> None:
        result = safe_eval("date.today()")
        assert isinstance(result, date)

    def test_timedelta(self) -> None:
        result = safe_eval("timedelta(days=1)")
        assert result.days == 1

    def test_relativedelta(self) -> None:
        result = safe_eval("relativedelta(months=2)")
        assert result.months == 2

    def test_min_max_len(self) -> None:
        assert safe_eval("max([1, 5, 3])") == 5
        assert safe_eval("min([1, 5, 3])") == 1
        assert safe_eval("len([1, 2, 3])") == 3


class TestLocalsAccess:
    def test_registry_access(self) -> None:
        registry = {"foo": object()}
        result = safe_eval("registry['foo']", {"registry": registry})
        assert result is registry["foo"]

    def test_attribute_access(self) -> None:
        class Stub:
            id = 42

        assert safe_eval("obj.id", {"obj": Stub()}) == 42


class TestSecurityRejections:
    def test_rejects_import(self) -> None:
        with pytest.raises(YamlConfigurationError, match="forbidden"):
            safe_eval("__import__('os')")

    def test_rejects_dunder_attribute(self) -> None:
        with pytest.raises(YamlConfigurationError, match="private attribute"):
            safe_eval("(1).__class__")

    def test_rejects_lambda(self) -> None:
        with pytest.raises(YamlConfigurationError, match="forbidden construct"):
            safe_eval("(lambda: 1)()")

    def test_rejects_undefined_name(self) -> None:
        with pytest.raises(YamlConfigurationError, match="undefined name"):
            safe_eval("os.system('ls')")

    def test_rejects_eval(self) -> None:
        with pytest.raises(YamlConfigurationError):
            safe_eval("eval('1+1')")

    def test_rejects_exec(self) -> None:
        with pytest.raises(YamlConfigurationError):
            safe_eval("exec('x=1')")

    def test_rejects_open(self) -> None:
        with pytest.raises(YamlConfigurationError):
            safe_eval("open('/etc/passwd').read()")

    def test_rejects_getattr(self) -> None:
        with pytest.raises(YamlConfigurationError):
            safe_eval("getattr(obj, '_private')", {"obj": object()})

    def test_rejects_syntax_error(self) -> None:
        with pytest.raises(YamlConfigurationError, match="Invalid"):
            safe_eval("1 +")

    def test_rejects_non_string(self) -> None:
        with pytest.raises(YamlConfigurationError, match="expects a string"):
            safe_eval(123)  # type: ignore[arg-type]


class TestRuntimeErrors:
    def test_runtime_error_wrapped(self) -> None:
        with pytest.raises(YamlConfigurationError, match="ZeroDivisionError"):
            safe_eval("1 / 0")
