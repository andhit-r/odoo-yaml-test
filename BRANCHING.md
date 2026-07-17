# Branching

One branch per Odoo series. A branch is honest about exactly one series: it calls that
series' ORM API directly and is tested only against it.

| Branch | Odoo series | Status | Consumers install with |
|---|---|---|---|
| `master` | 14.0 | active | `git+https://github.com/andhit-r/odoo-yaml-test.git@master` |
| `19.0` | 19.0 | active | `git+https://github.com/andhit-r/odoo-yaml-test.git@19.0` |

## `master` is the 14.0 branch and is never renamed

Production consumers pin `@master` — see `.github/workflows/test.yml` in every SSI addon
repo. Renaming or repointing `master` breaks all of their CI at once. New series get new
branches; `master` stays where it is.

There is deliberately **no `14.0` alias branch**. Two branches pointing at the same
content drift apart silently.

## What may differ between series branches

Only these. Everything else must stay byte-identical so `git cherry-pick` keeps working.

- `_refresh()` and `_savepoint()` in `case.py` — the ORM cache API is the one thing that
  genuinely changed across series (see table below).
- The `Form` import in `_get_form_class()` (`case.py`) — see "Where Form lives" below.
- `requires-python` in `pyproject.toml` **and** `setup.cfg` (they are not derived from
  each other).
- The `Framework :: Odoo :: <series>` classifier and the `Programming Language :: Python`
  classifiers in `pyproject.toml`.
- CI container image and Postgres service version in `.github/workflows/ci.yml`.
- The `test` job's `python-version` matrix in `.github/workflows/ci.yml` — it must follow
  `requires-python`. This is *not* optional bookkeeping: pip refuses to install the
  package on an out-of-range interpreter, so a matrix entry below `requires-python` fails
  before a single test runs. Odoo 19 needs Python ≥ 3.10 (`odoo/release.py`:
  `MIN_PY_VERSION = (3, 10)`), so the `19.0` branch drops the 3.8/3.9 entries `master`
  keeps.

### Where `Form` lives

| Series | Import | Note |
|---|---|---|
| 14.0 | `from odoo.tests.common import Form` | `Form` is defined in `common.py` |
| 18.0+ | `from odoo.tests import Form` | moved to `odoo/tests/form.py` |

`odoo.tests.common.Form` still *works* in 19 — `common.py` keeps a module-level
`__getattr__` shim that re-exports it — but it raises `DeprecationWarning` ("Since 18.0").
A branch that targets one series calls that series' API directly instead of leaning on a
back-compat shim; that is the same principle that forbids the `getattr` probe in
`_refresh()`.

### ORM cache API per series

| Series | flush | invalidate |
|---|---|---|
| 14.0 | `record.flush()` | `record.invalidate_cache(ids=[...])` |
| 16.0+ | `record.env.flush_all()` | `record.invalidate_recordset()` |

`flush()` and `invalidate_cache()` were deprecated in 16.0 and **removed in 17.0**.
Never probe for them with `getattr(...)` and fall back to doing nothing — that is
exactly how `_refresh()` became a silent no-op. If the expected API is absent, **raise**.
`_error_class()` and `_get_form_class()` already do this correctly; copy their shape.

**Do not "modernise" `flush_all()` into `flush_recordset()`.** It looks like the tighter,
more scoped call, and for the assert path it would be — but `search` refreshes through an
*empty* recordset (`env[model]`, `case.py::_action_search`), and `flush_recordset()` opens
with `if not self: return`. On an empty recordset it flushes **nothing**, so the search
would silently stop seeing prior writes. That is the same class of silent failure as the
`getattr` probe, just wearing a tidier hat. `flush_all()` serves both callers; an
over-broad *flush* is harmless because it only writes pending values out — unlike an
over-broad *invalidate*, which drops cached values and forces re-reads (see
`invalidate_all()` below).

Note the asymmetry, because it is the whole reason `_refresh` is shaped the way it is:

| Call | Breadth | Safe to over-apply? |
|---|---|---|
| `env.flush_all()` | env-wide | **Yes** — writes pending values out, drops nothing |
| `env.invalidate_all()` | env-wide | **No** — forces unrelated re-reads → AccessError after `as_user` |
| `record.invalidate_recordset()` | this recordset | Yes — scoped by construction |

## Source syntax stays aligned across branches — do not modernise

Every branch keeps `typing.Dict`/`List`/`Optional`, no PEP 604 `X | Y`, no walrus at
module scope — **even where the branch's `requires-python` would allow otherwise**.

This is not conservatism. Modernising a newer branch's syntax buys nothing but cosmetics
and makes every cherry-pick from `master` a conflict. The version-coupled surface is
~30 lines; keep it that way.

Enforced by config: `[tool.ruff] target-version = "py38"` must stay `py38` on **every**
branch. The lint set includes `"UP"` (pyupgrade); raising `target-version` makes ruff
silently rewrite `Dict[str, Any]` → `dict[str, Any]` across the whole source.

`setup.cfg` is kept on every branch for the same reason — it only exists for old
setuptools in Odoo 14 CI, but deleting it on newer branches would fork the file layout
for no gain.

## Fixes flow oldest → newest

Land a DSL fix on `master` (14.0) first, then forward-port:

```bash
git checkout 19.0
git cherry-pick <sha-from-master>
```

Conflicts should only ever appear in `_refresh()`/`_savepoint()` or packaging metadata.
A conflict anywhere else means the syntax-alignment rule above was broken — fix that,
do not paper over it.

## Tags and releases

Tags are global to the repo, so each series owns a namespace:

| Branch | Tag format | Example |
|---|---|---|
| `master` (14.0) | `vX.Y.Z` | `v0.5.0` |
| `19.0` and later | `<series>-vX.Y.Z` | `19.0-v0.1.0` |

`master` keeps the bare `vX.Y.Z` form it has used since `v0.4.0` — changing it would
orphan the existing release. Series tags do not match the `v*` glob, so they never
collide.

The version number itself lives in **three** places that are not derived from each
other: `pyproject.toml`, `setup.cfg`, `src/odoo_yaml_test/__init__.py`. Bump all three.
