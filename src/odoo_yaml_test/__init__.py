"""odoo-yaml-test: Data-driven unit testing framework for Odoo using YAML.

This package provides :class:`YamlTransactionCase`, a base test case that
executes scenarios declared in YAML files. It is designed to avoid the
architectural mistakes of Odoo's deprecated legacy YAML test mechanism by
keeping the YAML strictly declarative, isolating scenarios, providing
contextual error messages, and using only Odoo's public ORM API.

Example:
    >>> from odoo_yaml_test import YamlTransactionCase
    >>>
    >>> class TestSaleOrderYAML(YamlTransactionCase):
    ...     def test_b2b_scenario(self):
    ...         self.run_yaml_scenario("test_data.yaml")
"""

from typing import Any

from .exceptions import YamlAssertionError, YamlConfigurationError, YamlStepError

__version__ = "0.1.0"

__all__ = [
    "YamlAssertionError",
    "YamlConfigurationError",
    "YamlStepError",
    "YamlTransactionCase",
    "__version__",
]


def __getattr__(name: str) -> Any:
    """Lazy import for :class:`YamlTransactionCase`.

    Importing the class eagerly would require Odoo at install time, which
    breaks linting and unit-testing the package itself in non-Odoo
    environments. The lazy import defers the Odoo dependency until the
    class is actually referenced.
    """
    if name == "YamlTransactionCase":
        from .case import YamlTransactionCase

        return YamlTransactionCase
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
