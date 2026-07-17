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
- `requires-python` in `pyproject.toml` **and** `setup.cfg` (they are not derived from
  each other).
- The `Framework :: Odoo :: <series>` classifier in `pyproject.toml`.
- CI container image and Postgres service version in `.github/workflows/ci.yml`.

### ORM cache API per series

| Series | flush | invalidate |
|---|---|---|
| 14.0 | `record.flush()` | `record.invalidate_cache(ids=[...])` |
| 16.0+ | `record.env.flush_all()` | `record.invalidate_recordset()` |

`flush()` and `invalidate_cache()` were deprecated in 16.0 and **removed in 17.0**.
Never probe for them with `getattr(...)` and fall back to doing nothing — that is
exactly how `_refresh()` became a silent no-op. If the expected API is absent, **raise**.
`_error_class()` and `_get_form_class()` already do this correctly; copy their shape.

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
