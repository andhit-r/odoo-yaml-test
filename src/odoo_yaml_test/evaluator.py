"""Safe evaluator for ``EVAL:`` expressions in YAML scenarios.

The evaluator first attempts :func:`ast.literal_eval` (the safest possible
path). If that fails, it walks the AST and rejects any node that is not
explicitly whitelisted, then evaluates the expression with an empty
``__builtins__`` and a restricted globals dictionary.

Even with these protections, ``EVAL:`` should be considered a *trusted*
feature: the YAML author can still call methods on objects exposed
through ``locals_dict`` (notably ``self``, ``env``, and ``registry``).
The whitelist exists to prevent accidental damage and obvious sandbox
escapes, not to make untrusted YAML safe to execute.
"""

from __future__ import annotations

import ast
import datetime as _datetime
from decimal import Decimal
from typing import Any, Mapping

try:  # python-dateutil is a runtime dep; keep import resilient for typing-only contexts
    from dateutil.relativedelta import relativedelta
except ImportError:  # pragma: no cover - dateutil is in install_requires
    relativedelta = None  # type: ignore[assignment,misc]

from .exceptions import YamlConfigurationError

#: Names that may appear in an ``EVAL:`` expression.
_ALLOWED_GLOBALS: dict[str, Any] = {
    "datetime": _datetime.datetime,
    "date": _datetime.date,
    "time": _datetime.time,
    "timedelta": _datetime.timedelta,
    "Decimal": Decimal,
    "True": True,
    "False": False,
    "None": None,
    "len": len,
    "range": range,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "round": round,
}
if relativedelta is not None:
    _ALLOWED_GLOBALS["relativedelta"] = relativedelta

#: Callables that must never be reachable, even if a name happens to be
#: re-exported through a whitelisted module.
_FORBIDDEN_CALLABLE_NAMES: frozenset[str] = frozenset(
    {
        "__import__",
        "compile",
        "delattr",
        "eval",
        "exec",
        "exit",
        "getattr",
        "globals",
        "hasattr",
        "input",
        "locals",
        "open",
        "quit",
        "setattr",
        "vars",
    }
)


def _validate_ast(tree: ast.AST, allowed_names: frozenset[str]) -> None:
    """Walk *tree* and reject any node that is not safe.

    Raises:
        YamlConfigurationError: when the expression contains a forbidden
            construct (imports, dunder access, forbidden builtins, etc.).
    """
    for node in ast.walk(tree):
        # Reject all import-like and statement-level constructs outright.
        if isinstance(
            node,
            (
                ast.Import,
                ast.ImportFrom,
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Lambda,
                ast.Global,
                ast.Nonlocal,
                ast.Delete,
                ast.With,
                ast.AsyncWith,
                ast.Try,
                ast.Raise,
                ast.Assert,
                ast.Yield,
                ast.YieldFrom,
                ast.Await,
            ),
        ):
            raise YamlConfigurationError(
                f"EVAL expression contains forbidden construct: {type(node).__name__}"
            )

        if isinstance(node, ast.Attribute) and node.attr.startswith("_"):
            raise YamlConfigurationError(
                f"EVAL expression accesses private attribute: {node.attr!r}"
            )

        if isinstance(node, ast.Name):
            if node.id in _FORBIDDEN_CALLABLE_NAMES:
                raise YamlConfigurationError(
                    f"EVAL expression references forbidden name: {node.id!r}"
                )
            if node.id not in allowed_names:
                raise YamlConfigurationError(
                    f"EVAL expression references undefined name: {node.id!r}. "
                    f"Allowed names: {sorted(allowed_names)}"
                )

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in _FORBIDDEN_CALLABLE_NAMES
        ):
            raise YamlConfigurationError(
                f"EVAL expression calls forbidden function: {node.func.id!r}"
            )


def safe_eval(expression: str, locals_dict: Mapping[str, Any] | None = None) -> Any:
    """Safely evaluate *expression* with a restricted namespace.

    Args:
        expression: A Python expression (no statements). The string must
            already have the ``EVAL:`` prefix stripped by the caller.
        locals_dict: Names made available to the expression (typically
            ``self``, ``env``, ``registry``).

    Returns:
        The result of evaluating the expression.

    Raises:
        YamlConfigurationError: when the expression is syntactically
            invalid or violates the whitelist.

    Example:
        >>> safe_eval("1 + 2")
        3
        >>> from datetime import datetime
        >>> isinstance(safe_eval("datetime.now()"), datetime)
        True
    """
    if not isinstance(expression, str):
        raise YamlConfigurationError(f"safe_eval expects a string, got {type(expression).__name__}")

    locals_dict = dict(locals_dict or {})

    # Fast path: pure-literal expressions never need our restricted eval.
    try:
        return ast.literal_eval(expression)
    except (ValueError, SyntaxError):
        pass

    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError as exc:
        raise YamlConfigurationError(f"Invalid EVAL expression {expression!r}: {exc}") from exc

    allowed_names = frozenset(_ALLOWED_GLOBALS.keys()) | frozenset(locals_dict.keys())
    _validate_ast(tree, allowed_names)

    compiled = compile(tree, filename="<EVAL>", mode="eval")
    # CPython's C-level constructors (e.g. `date.today()`, `datetime.now()`)
    # may internally call `__import__` to load stdlib modules like `time`.
    # We expose the real `__import__` here because the AST validator above
    # already rejects every `import` statement and call to the `__import__`
    # name at parse time — by the time we reach this point, the only callers
    # of `__import__` left are CPython's own C code.
    safe_builtins: dict[str, Any] = {"__import__": __import__}
    globals_dict: dict[str, Any] = {"__builtins__": safe_builtins, **_ALLOWED_GLOBALS}
    try:
        return eval(compiled, globals_dict, locals_dict)  # noqa: S307 - sandboxed eval
    except YamlConfigurationError:
        raise
    except Exception as exc:
        raise YamlConfigurationError(
            f"EVAL expression {expression!r} raised {type(exc).__name__}: {exc}"
        ) from exc
