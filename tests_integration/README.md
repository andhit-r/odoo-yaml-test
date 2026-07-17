# Integration tier

Everything under `tests/` runs against hand-rolled fakes and needs no Odoo — that is a
deliberate constraint (see `CLAUDE.md`, "Odoo is an optional import"). This tier is the
opposite: it runs `YamlTransactionCase` against a **real Odoo registry and database**.

It is excluded from the default `pytest` run (`testpaths = ["tests"]` in
`pyproject.toml`) and executes only in the `integration` CI job, inside an OCA CI image
that already ships Odoo. Nothing here is packaged into the wheel or sdist.

## Why it exists

The fakes in `tests/` implement `flush()` / `invalidate_cache()` — the Odoo 14 ORM API,
the same one `case.py` assumes. Fake and code always agree, so that suite can never
detect the API drifting out from under the library. This tier is the only thing that
can.

## `yaml_test_probe`

A throwaway addon whose only job is to make `_refresh()` observable. `mirror` is a
non-stored compute declared with an empty `@api.depends()`, so Odoo has no trigger that
would ever invalidate it when `source` changes. That is exactly the staleness
`_refresh()` exists to clear — and exactly what goes undetected if `_refresh()` silently
stops working.

The scenario reads `mirror` (caching it), writes `source`, then asserts `mirror` again
with `refresh: true`. If `_refresh()` works, the assert sees the new value. If it is a
no-op, the assert sees the stale one and the job goes red.
