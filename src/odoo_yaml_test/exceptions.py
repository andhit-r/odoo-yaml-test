"""Custom exception types raised by odoo-yaml-test."""

from __future__ import annotations


class YamlTestError(Exception):
    """Base class for all errors raised by odoo-yaml-test."""


class YamlConfigurationError(YamlTestError):
    """Raised when a YAML file is structurally invalid.

    Examples include missing required keys, unknown actions, or values
    that cannot be parsed.
    """


class YamlStepError(YamlTestError):
    """Raised when an individual step fails during execution.

    The message includes file, scenario, step, and the original error so
    a developer can locate the failure quickly.
    """


class YamlAssertionError(AssertionError):
    """Raised when an ``assert`` action fails.

    Inherits from :class:`AssertionError` so unittest treats it as a
    test failure rather than an error.
    """
