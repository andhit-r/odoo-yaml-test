# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed
- **This branch targets Odoo 19.0.** `_refresh()` now calls `env.flush_all()` and
  `invalidate_recordset()` directly instead of probing for the 14.0 names
  (`flush`/`invalidate_cache`), which Odoo 17 removed. The probe was never a
  compatibility shim: because it fell back to doing nothing, `_refresh()` degraded into a
  silent no-op on 17+, so `refresh: true` stopped clearing stale non-stored computes
  without any error. There is no fallback here by design — a missing API now raises.
  `requires-python` is `>=3.10` (Odoo 19's own floor) and the CI `test` matrix follows it.
- **`action: form` imports `Form` from `odoo.tests`, not `odoo.tests.common`.** The latter
  still works in 19 through a deprecation shim, but a branch that targets one series calls
  that series' API directly. No YAML change: this is internal to `_get_form_class()`.

### Migrating scenarios from a 14.0 module
- **Steps that call `invalidate_cache` break here.** `action: call` with
  `method: "invalidate_cache"` — the spelling used by the manual cache-drop steps across
  the SSI corpus — raises `AttributeError` on Odoo 19, because the ORM removed that method
  in 17. Rename it to `invalidate_recordset`, or better, delete the step and set
  `refresh: true` on the assert that needed it; handling this is what the library is for.
  Everything else in the DSL is unchanged — actions, prefixes, assertion types, and
  operators all behave identically to `master`.

### Added
- **An integration tier that runs against a real Odoo 14 registry.** Everything under
  `tests/` drives hand-rolled fakes — and those fakes implement `flush()` /
  `invalidate_cache()`, the very API `case.py` assumes, so fake and code always agreed
  and the suite could never notice the ORM drifting out from under it. The new
  `tests_integration/` tier installs a throwaway `yaml_test_probe` addon whose
  non-stored compute Odoo never invalidates, which makes `_refresh()` observable: if it
  silently stops working, the assert reads a stale value and CI goes red. Excluded from
  the default `pytest` run; executes only in the `integration` CI job. Odoo remains an
  optional import and is still absent from `dependencies`.

### Changed
- **Documentation now states the target Odoo series honestly.** The README claimed the
  library "should work on later versions as well" because it only touches Odoo's public
  ORM API. That claim was false: `flush()` and `invalidate_cache()` — both public API in
  14.0 — were removed in Odoo 17, which silently turns `_refresh()` into a no-op there.
  Nothing changed in the code; the claim was the bug. `master` is now documented as the
  14.0 series branch.
- **`CLAUDE.md` no longer describes a PyPI release flow that does not exist.** Releases
  have created a GitHub Release via `release.yml` since `04baa38`; the guidance still
  pointed at a `publish.yml` with PyPI OIDC publishing.
- **CI and release triggers now accept series branches.** `ci.yml` fired only on
  `master`, so a series branch cut from it would have inherited a filter that never
  matches — silently no CI. `release.yml` now also accepts `<series>-v*` tags, so series
  releases do not collide with `master`'s existing `vX.Y.Z` namespace. Both changes are
  additive: `master` pushes and `v*` tags behave exactly as before. Policy is documented
  in the new `BRANCHING.md`.

### Fixed
- **`asserts` inside `action: form` no longer crash on any field.** `_read_path`
  detected "empty container" via `hasattr(current, "__len__")`. A real Odoo
  `Form` (and the library's `_FormProxy` wrapper around one) raises
  `AssertionError` — not `AttributeError` — from its own `__getattr__` for any
  name absent from the view, including dunder probes like `__len__`.
  `hasattr()` only swallows `AttributeError`, so the probe's `AssertionError`
  escaped uncaught, breaking `asserts:` (and op-level `- assert:`) inside
  *every* `action: form` step regardless of which field was being asserted.
  The check now calls `len()` directly and catches both `TypeError` (the
  ordinary "no `__len__`" case) and `AssertionError` (the Form quirk).

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

- **Opt-in cache refresh** (`auto_refresh`, default **off**). When enabled, the
  record is flushed and its cache invalidated before an assertion, so
  non-stored computes — SSI's `confirm_ok`, `approve_ok`, … — are recomputed
  rather than read stale. This is what the 533 manual `invalidate_cache` steps
  across the SSI corpus are working around.

  Enable per file with `options: {auto_refresh: true}`, per scenario, per step
  (`refresh: true`), or with `auto_refresh = True` on the test class.

  **It is off by default on purpose.** Invalidating forces the field to be
  re-read from the database, and a re-read runs the record rules that a cached
  value never had to pass. So switching it on surfaces asserts that only ever
  passed because nobody re-read the value. Measured against real Odoo:
  `ssi_school` goes from 0 to **15 errors out of 79 tests**, all `AccessError`,
  because its scenarios act `as_user: base.user_admin` on records that user
  cannot actually read. Those tests are wrong and worth fixing — but that is
  each module owner's call to make on their own schedule, not something a
  library upgrade should force. Nothing changes until you opt in.

  The invalidation is scoped to the asserted record's ids. Odoo's bare
  `invalidate_cache()` empties the cache for the whole environment, which drags
  unrelated records into the same re-read (and the same access checks).

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
