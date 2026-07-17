# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`odoo-yaml-test` is a standalone PyPI library (not an Odoo addon) that provides
`YamlTransactionCase` — an Odoo `TransactionCase` subclass that executes test scenarios
declared in YAML instead of Python boilerplate. It is a deliberate redesign of Odoo's
legacy `test/*.yml` mechanism (deprecated after Odoo 11), keeping the YAML strictly
declarative (no control flow), isolating each scenario in its own `subTest` with a fresh
registry, and touching only Odoo's public ORM API.

## Commands

```bash
pip install -e ".[dev]"      # setup
pytest                       # full suite (coverage is on by default via addopts)
pytest tests/test_evaluator.py::test_rejects_import -x   # single test
pre-commit run --all-files   # ruff (lint+format), mypy --strict on src/, gitleaks
mypy src                     # types only
python -m build              # sdist + wheel
```

Releases create a **GitHub Release** (not PyPI) on a pushed `v*` tag, via the reusable
workflow `andhit-r/github-release@v1` (`.github/workflows/release.yml`). Bump the
version in **three** places, they are not derived from each other: `pyproject.toml`,
`setup.cfg`, and `__init__.__version__`.

## Architecture

Four modules under `src/odoo_yaml_test/`, in dependency order:

- `exceptions.py` — the error taxonomy that everything else keys off. `YamlConfigurationError`
  means *the YAML is wrong* (bad schema, unknown action, forbidden EVAL). `YamlStepError` means
  *a step blew up at runtime*. `YamlAssertionError` deliberately inherits from `AssertionError`
  so unittest reports it as a **failure**, not an error — preserve that lineage.
- `loader.py` — `yaml.safe_load` only (never `yaml.load`), plus schema validation of the
  `scenarios: [{name, steps: [...]}]` shape.
- `evaluator.py` — the `EVAL:` sandbox. Tries `ast.literal_eval` first, then AST-walks the
  expression rejecting imports/lambdas/defs/dunder-attribute access/forbidden builtins, then
  `eval`s with empty builtins and a whitelisted globals dict.
- `case.py` — `YamlTransactionCase`. Everything else feeds into it.

### Odoo is an optional import

`case.py` wraps `from odoo.tests.common import TransactionCase` in a `try/except ImportError`
and falls back to a placeholder base class that raises on instantiation. This is what lets
CI lint, type-check, and unit-test the package **without an Odoo installation**. Never make
Odoo a hard import at module scope, and never add `odoo` to `dependencies`.

Consequently, `tests/` cannot exercise a real ORM. `tests/test_case_smoke.py` drives the
action handlers against hand-rolled fake `env`/recordset objects. When you add an action or
assertion type, extend those fakes rather than reaching for a live database.

### The two extension points are named by convention

`_dispatch_step` resolves `action: foo` to `self._action_foo` via `getattr`; `_run_asserts`
resolves `type: bar` to `self._assert_bar` the same way. Adding an action or assertion type
means adding a correspondingly-named method — there is no registry to update. Subclasses in
consuming projects can add their own the same way.

### Dynamic value resolution

`_resolve_values` calls `model.fields_get(..., attributes=["type"])` so it knows each field's
ORM type before resolving, which is what makes *implicit* xml_id resolution safe: a bare
`"module.some_id"` string is resolved via `env.ref` **only** when the field is relational
(`_RELATIONAL_TYPES`) and matches `_XML_ID_RE`. Non-relational strings pass through verbatim.
Explicit prefixes (`EVAL:`, `REF:`, `RECORDSET:`) work anywhere and recurse into nested
lists/dicts, including `args`, `kwargs`, `domain`, and `context`.

`EVAL:` is trusted-input-only by design. The whitelist prevents accidents and obvious escapes,
but a YAML author still has `self`/`env`/`registry` and can mutate the database. Treat YAML
files as test code, not as data. Note the intentional exception in `safe_eval`: real
`__import__` **is** exposed in the eval globals because CPython's C-level constructors
(`datetime.now()`) need it internally — this is safe only because the AST validator already
rejected every `import` statement and `__import__` reference before we get there. Don't
"fix" that without also re-reading the AST validator.

## Python version constraint

`requires-python = ">=3.10"` here (and duplicated in `setup.cfg`, which is not derived from
it — bump both). 3.10 is Odoo 19's own floor: `odoo/release.py` sets
`MIN_PY_VERSION = (3, 10)`. The `test` matrix in `ci.yml` must not list interpreters below
it, or pip refuses to install before any test runs.

`setup.cfg` is kept even though this branch does not need old setuptools — deleting it here
would fork the file layout away from `master` for no gain.

**Source must nevertheless stay import-time compatible with old Pythons**: no walrus in
module scope, no PEP 604 `X | Y` annotations, use `typing.Dict`/`List`/`Optional` in
signatures, and keep `[tool.ruff] target-version = "py38"`. That looks pointless on a 3.10+
branch and is not: the syntax is what keeps `git cherry-pick` from `master` conflict-free,
and `target-version` is what stops ruff's `UP` rules from rewriting it all automatically.
Local variable annotations like `kwargs: dict[str, Any] = {}` are fine (never evaluated).

## This branch targets one Odoo series

This is the **Odoo 19.0** series branch. It is not version-agnostic and must not pretend
to be: `_refresh()` in `case.py` calls 19's ORM API (`env.flush_all()`,
`invalidate_recordset()`) directly, with no `getattr` probe and no fallback — a missing
API must raise, never degrade into a silent no-op. Consumers install it with
`pip install git+https://github.com/andhit-r/odoo-yaml-test.git@19.0`.

`master` is the **Odoo 14.0** branch and is never renamed or repointed — production
consumers pin `@master`. Fixes flow oldest → newest: land a DSL fix on `master` first,
then `git cherry-pick` it here. That stays cheap only while the source syntax remains
aligned, so **do not modernise this branch** (no PEP 604, keep `typing.Dict`/`List`/
`Optional`, keep `[tool.ruff] target-version = "py38"`) even though `requires-python` is
3.10 here. See `BRANCHING.md` for the full list of what may legitimately differ.

## Docs are part of the contract

`README.md` (action table, prefixes, security section) and `docs/usage.md` (the authoritative
YAML schema reference) both document the format. Any change to actions, assertion types,
operators, or prefixes must land in both, plus `CHANGELOG.md`.
