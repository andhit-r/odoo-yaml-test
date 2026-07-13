# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.4.0] - 2026-07-13

Everything in this release is additive. No existing key, prefix, action, or
assertion type changed meaning — YAML written against 0.1.0 keeps working.

### Fixed
- **Assertion failures now report where they happened.** `_run_scenario` caught
  `YamlAssertionError` and re-raised it bare, so the file/scenario/step context
  promised in the README was added to *every* failure except the most common
  one. Failures still surface as `AssertionError` (unittest reports FAIL, not
  ERROR); they simply say which step failed now.

### Added
- **`REC:` prefix** for referencing records saved earlier in a scenario.
  `REC: so_1` yields the record, `REC: so_1.id` the integer id, and
  `REC: so_1.partner_id.name` walks the relation. This replaces the
  `EVAL: registry['so_1'].id` spelling, which accounted for 87% of all `EVAL:`
  use across the SSI corpus. `EVAL:` is *not* deprecated — it remains the tool
  for arithmetic, dates, and anything needing real Python.
- **Built-in registry aliases** `company` and `user`, seeded from `env.company`
  and `env.user` at the start of every scenario. A `save_as` of the same name
  still wins.
- **Recordsets are coerced to what a field expects** in `create`/`write`: a
  record assigned to a `many2one` becomes its id; to an x2many, a `(6, 0, ids)`
  command. Only applied where the field type is known — never in domains, args,
  or kwargs.
- **Dynamic prefixes are now allowed in `asserts`.** Previously documented as
  unsupported. This revives the relational assertion types: `m2o` accepts
  `expected: "REC: partner"` alongside the existing `expected_xml_id`, and
  `o2m`/`m2m` gain `check: contains` / `check: exact` with `expected_records`.
- **Dotted paths in assertion field names** — `partner_id.name:` reads across a
  relation. Traversing an empty relation is a test failure, not a config error.
- **`as_user` accepts a registry alias**, not just an xml_id — so a user created
  inside the scenario can be used directly. Previously the only way to test as a
  non-admin user was to fabricate an `ir.model.data` row mid-scenario.
- **`as_user` and `context` now apply to `assert` and `ref`**, matching every
  other action. SSI policy fields are user-sensitive, so this was a real gap.
- **`expect_error`** on `create` / `write` / `call` / `unlink` / `wizard` /
  `form`, with an optional `message_contains`. The block runs inside a cursor
  savepoint, so an expected `ValidationError` no longer poisons the rest of the
  transaction. Exception names resolve from a fixed whitelist.
- **`unlink` action**, so delete guards and `force_unlink` are reachable.
- **`wizard` action** — creates a transient with `active_model` / `active_id` /
  `active_ids` in the context the way the UI does, then calls a method on it.
  This makes SSI's cancel/terminate reason wizards testable; an explicit
  `context:` still wins over the defaults. Note the target key is `target:`, not
  `on:` — YAML 1.1 parses a bare `on:` key as the boolean `True`.
- **`form` action** — drives Odoo's `Form` API, which is the only way to
  exercise `@api.onchange`. Supports an ordered `values:` mapping for the common
  case and an `ops:` list (`set` / `new` / `edit` / `remove` / `assert`) when
  assignment order matters, a field must be set twice to re-fire an onchange, or
  x2many lines are built inline. `asserts` run against the pending form, before
  `save()`. Note that form values take *records*, not ids — `partner_id:
  "REC: p"` (bare, no `.id`), unlike `create`.
- **Top-level `setup:` block**, replayed before each scenario against a freshly
  reset registry — shared fixtures without shared state. Opt out per scenario
  with `skip_setup: true`.

### Changed
- **Asserts now read post-flush, post-invalidation values by default.** Before
  an assertion (and before `search`), the record is flushed and its cache
  dropped. SSI's policy fields (`confirm_ok`, `approve_ok`, …) are non-stored
  computes that Odoo does not invalidate when `state` changes, which is why
  scenarios were littered with manual `invalidate_cache` steps; those steps
  remain valid but are now redundant.

  **If a scenario starts failing after this upgrade, the assertion was
  previously passing against a stale cache.** Opt out, in order of precedence:
  `refresh: false` on a step, `options: {auto_refresh: false}` on a scenario or
  file, or `auto_refresh = False` on the test class.

## [0.1.0] - 2026-05-01

### Added
- Initial public release.
- `YamlTransactionCase` base class with auto-discovery of YAML files
  via `inspect.getfile`.
- Six declarative actions: `create`, `write`, `call`, `assert`, `ref`,
  `search`.
- Assertion types: `value` (with comparison operators), `m2o`, `o2m`,
  `m2m`.
- Dynamic value resolution prefixes: `EVAL:`, `REF:`, `RECORDSET:`,
  plus implicit xml_id resolution for relational fields.
- AST-validated `safe_eval` with restricted globals and forbidden-name
  blocklist.
- Contextual error wrapping that includes file, scenario, step, and
  original exception.
- Per-scenario `subTest` isolation and per-scenario registry reset.
- PEP 561 type marker (`py.typed`).
