# YAML Schema Reference

This document is the authoritative reference for the YAML format
consumed by `YamlTransactionCase`. For a quick tour, see the
project [README](../README.md).

## Top-Level Structure

```yaml
options:               # optional; file-wide settings
  auto_refresh: true

fake_models:           # optional; throwaway models for testing a mixin
  classes:
    - "odoo.addons.my_module.tests.fake_models:TestConsumer"

setup:                 # optional; replayed before EVERY scenario
  steps:
    - step: "Create journal"
      action: "create"
      # ...

scenarios:
  - name: "Human-readable scenario name"
    options: {}        # optional; overrides the file-level options
    skip_setup: false  # optional
    steps:
      - step: "Human-readable step label"
        action: "create"
        # ... action-specific keys
```

The top-level mapping must contain a `scenarios` key whose value is a
list of scenario mappings. Each scenario must contain `name` (string)
and `steps` (list).

Each step must contain `action` and a `step` label (used in error
messages and logs). Other keys depend on the action.

### `setup`

Setup steps run before each scenario, into the freshly reset registry. They
are not run once per file: re-executing them per scenario is what preserves
the "no cross-scenario state leakage" guarantee while removing the
copy-paste. A scenario can opt out with `skip_setup: true`.

A failure inside a setup step is reported as `-> Setup Step: '...'`, so it is
never mistaken for a failure in the scenario body.

### `options`

| Key            | Default | Meaning                                       |
| -------------- | ------- | --------------------------------------------- |
| `auto_refresh` | `true`  | Flush + invalidate the cache before each assert |

Precedence, highest first: a step's `refresh:` key, the scenario's `options`,
the file's `options`, the test class's `auto_refresh` attribute.

### `fake_models`

Declares model classes to add to the live Odoo registry for the duration of the
test method. This is how you test an `AbstractModel`: a mixin has no table, so
something concrete must inherit it before a scenario can `create()` anything.

Short form — a list of class references:

```yaml
fake_models:
  - "odoo.addons.my_module.tests.fake_models:TestConsumer"
```

Long form, with options:

```yaml
fake_models:
  classes:
    - "odoo.addons.my_module.tests.fake_models:MyMixin"
    - "odoo.addons.my_module.tests.fake_models:TestConsumer"
  acl: true
  groups: ["base.group_user"]
  addon: "my_module"
```

| Key       | Default                | Meaning                                              |
| --------- | ---------------------- | ---------------------------------------------------- |
| `classes` | *(required)*           | `"module.path:ClassName"` references, in load order   |
| `acl`     | `true`                 | Create full RWCU `ir.model.access` rows               |
| `groups`  | `["base.group_user"]`  | Groups the generated ACL is granted to                |
| `addon`   | derived from test class | Addon the models are attributed to                   |

References are **strings, not Python imports** — see the warning below for why
that is not merely a stylistic choice.

**Lifecycle.** The models are loaded once per file, before the first scenario
(loading runs DDL, far too expensive to repeat per scenario). They are unloaded
at the end of the **test method**, via `addCleanup`. Per-scenario isolation is
unaffected: rows in a fake table roll back with the test transaction like any
other. Because unloading is per method, one test method may load `fake_models:`
from only one file; a second raises rather than silently overwriting.

**ACL.** A fake model has no `security/ir.model.access.csv`. The default env is
superuser and would never notice, but any `as_user:` step would fail with an
`AccessError` that says nothing about what you are actually testing. Set
`acl: false` if you want to create the rows yourself.

> **The class module must not be imported while the addon loads.** Keep it out
> of `tests/__init__.py`, and do not import it at the top of your test file.
> Odoo builds every model class it sees at load time, so an early import turns
> the fake model into a real one — with a real table, in every database that
> installs your addon. Naming the class as a string is what defers the import
> to the right moment. The library refuses to run when it detects that this has
> already happened.

Example fixture module (`my_module/tests/fake_models.py`):

```python
from odoo import fields, models


class MyMixin(models.AbstractModel):
    _name = "my.mixin"
    _description = "The mixin under test"

    state = fields.Selection([("draft", "Draft"), ("done", "Done")], default="draft")

    def action_done(self):
        self.write({"state": "done"})
        return True


class TestConsumer(models.Model):
    _name = "my.mixin.consumer"
    _inherit = "my.mixin"
    _description = "Exists only inside a test transaction"

    name = fields.Char(required=True)
```

## Actions

### `create`

Creates a new record.

```yaml
- step: "Create partner"
  action: "create"
  model: "res.partner"
  save_as: "p1"            # optional; alias under self.registry
  context:                 # optional; merged into env.context
    lang: "id_ID"
  as_user: "base.user_admin"  # optional; runs with this user
  values:
    name: "Acme"
    parent_id: "REF: base.main_partner"
```

### `write`

Updates a record already in the registry.

```yaml
- step: "Rename partner"
  action: "write"
  target: "p1"
  values:
    name: "Acme Corp"
```

### `call`

Invokes a method on a registered record. May include `args`, `kwargs`,
and post-call `asserts`.

```yaml
- step: "Confirm order"
  action: "call"
  target: "so_1"
  method: "action_confirm"
  args: []
  kwargs: {}
  asserts:
    state:
      type: "value"
      expected: "sale"
```

### `assert`

Runs assertions on a registered record without performing any action.

```yaml
- step: "Validate"
  action: "assert"
  target: "so_1"
  asserts:
    partner_id:
      type: "m2o"
      expected_xml_id: "base.res_partner_1"
```

### `unlink`

Deletes a registered record. Pair it with `context: {force_unlink: true}` to
exercise delete guards.

```yaml
- step: "Delete the draft"
  action: "unlink"
  target: "doc"
```

### `wizard`

Creates a transient model with `active_model` / `active_id` / `active_ids` in
the context — the way the web client does — and optionally calls a method on
it. This is what makes reason-picker wizards (cancel, terminate) reachable.

```yaml
- step: "Cancel through the reason wizard"
  action: "wizard"
  model: "base.select_cancel_reason"
  target: "amortization"        # the DOCUMENT; supplies the active_* context
  values:
    cancel_reason_id: "REC: reason"
  method: "action_confirm"
  asserts:                      # asserted on the document, not the wizard
    state:
      type: "value"
      expected: "cancel"
```

The key is `target:`, **not** `on:` — YAML 1.1 parses a bare `on:` key as the
boolean `True`. An explicit `context:` on the step overrides the `active_*`
defaults.

### `form`

Drives Odoo's `Form` API. This is the only way to exercise `@api.onchange`:
onchange methods do not run through `create()` or `write()`.

Either `model:` (a new record) or `target:` (edit an existing one), never
both.

```yaml
- step: "Onchange clears the type"
  action: "form"
  model: "appointment_schedule"
  save_as: "sched"
  values:                          # ordered; each assignment may fire onchange
    title: "Test"
    appointee_id: "REC: user_a"    # a RECORD, not an id
  asserts:                         # run on the form, BEFORE save()
    type_id:
      type: "value"
      operator: "is_falsy"
  save: true                       # default; set false to never create
```

`values:` is sugar for a sequence of `set` ops in YAML order. When order
matters, a field must be assigned twice to re-fire an onchange, or x2many
lines are built inline, use `ops:` (mutually exclusive with `values:`):

```yaml
  ops:
    - set: {appointee_id: "REC: user_a"}
    - set: {type_id: "REC: appt_type"}
    - set: {appointee_id: "REC: user_a"}   # re-fire the onchange
    - assert: {type_id: {type: "value", operator: "is_falsy"}}
    - new:
        field: "order_line"
        values: {product_id: "REC: product", product_uom_qty: 2}
    - edit: {field: "order_line", index: 0, values: {product_uom_qty: 5}}
    - remove: {field: "order_line", index: 0}
```

**Note the one wart, inherent to the Form API:** form values take *records*,
so you write `partner_id: "REC: p"` (bare). In `create`/`write` you write
`partner_id: "REC: p"` too, but there the record is coerced to an id for you.

`save_as` requires `save: true` — there is otherwise no record to store.

### `ref`

Resolves a single `xml_id` and stores the resulting record.

```yaml
- step: "Resolve admin user"
  action: "ref"
  xml_id: "base.user_admin"
  save_as: "admin"
```

### `search`

Performs a search with a domain and stores the resulting recordset.

```yaml
- step: "Find draft orders"
  action: "search"
  model: "sale.order"
  domain:
    - ["state", "=", "draft"]
  save_as: "drafts"
  limit: 10
  order: "date_order desc"
  expect_count: 3        # optional; immediate sanity check
```

## Assertion Types

### `value`

Direct comparison via the chosen `operator`.

```yaml
asserts:
  amount_total:
    type: "value"
    operator: "gt"        # equals, not_equals, gt, gte, lt, lte,
                          # in, not_in, contains, is_truthy, is_falsy
    expected: 0
```

When `operator` is `is_truthy` or `is_falsy`, the `expected` key is
ignored.

### Dotted field names

An assertion key may traverse relations. No Odoo field name contains a dot, so
a dot always means traversal.

```yaml
asserts:
  partner_id.name:
    type: "value"
    expected: "Acme"
  type_id.journal_id.code:
    type: "value"
    expected: "TAMRJ"
```

Traversing through an empty relation is a test *failure*, not a
configuration error.

### `m2o`

Compares a many-to-one field's id against either a record (any dynamic
prefix, or a raw id) or an xml_id. Supplying both is an error.

```yaml
asserts:
  partner_id:
    type: "m2o"
    expected: "REC: customer"          # a record created earlier
  # or, for a record that has an xml_id:
  # expected_xml_id: "base.res_partner_1"
```

### `o2m` and `m2m`

```yaml
# Count check
asserts:
  order_line:
    type: "o2m"
    check: "count"
    expected_count: 3

# Subset check (all listed records must be present)
asserts:
  order_line:
    type: "o2m"
    check: "contains"
    expected_records:
      - "REC: line_1"
      - "REC: line_2"

# Exact set check (order-insensitive equality)
asserts:
  tag_ids:
    type: "m2m"
    check: "exact"
    expected_records: ["REC: tag_a"]
```

The xml_id-only spellings `contains_xml_ids` / `exact_xml_ids` (with
`expected_xml_ids`) remain valid and mean the same thing.

## Expected Errors

Any of `create`, `write`, `call`, `unlink`, `wizard`, and `form` accepts an
`expect_error` mapping. The step must then raise the named exception, or the
scenario fails.

```yaml
- step: "Duplicate code must be refused"
  action: "create"
  model: "account.amortization_type"
  expect_error:
    type: "UserError"
    message_contains: "duplicate"     # optional substring, case-sensitive
  values:
    name: "Dup"
    code: "TAMRT01"
```

`type` must name one of: `AccessDenied`, `AccessError`, `CacheMiss`,
`MissingError`, `RedirectWarning`, `UserError`, `ValidationError`,
`AssertionError`, `AttributeError`, `KeyError`, `TypeError`, `ValueError`.

The step runs inside a cursor savepoint, so the failed write rolls back and
later steps still run against a clean transaction. `expect_error` cannot be
combined with `save_as` — the step is expected to fail, so there is no record
to save.

## Running as Another User

`as_user` is accepted by every action. It takes an xml_id, a dynamic prefix,
or — most usefully — a plain registry alias, so a user created inside the
scenario can be used without inventing an xml_id for it:

```yaml
- step: "Create an approver"
  action: "create"
  model: "res.users"
  save_as: "approver"
  values:
    name: "Approver"
    login: "approver@example.com"
    groups_id: [[6, 0, ["REF: base.group_user"]]]

- step: "Approve as that user"
  action: "call"
  target: "doc"
  method: "action_approve_approval"
  as_user: "approver"          # <- the alias, not an xml_id
```

## Dynamic Value Prefixes

Usable in `values`, `args`, `kwargs`, `domain`, `context`, and inside
`asserts` (including `expected`).

| Prefix              | Behaviour                                                                  |
| ------------------- | -------------------------------------------------------------------------- |
| `REC: <alias>`      | The record saved under `<alias>` earlier in this scenario.                 |
| `REC: <alias>.<path>` | A plain getattr chain: `REC: so.id`, `REC: so.partner_id.name`. No calls, indexes, or operators — use `EVAL:` for those. |
| `EVAL: <expr>`      | Evaluates `<expr>` in a restricted namespace (see Security in README).     |
| `REF: <xid>`        | Returns `env.ref(xid).id` (integer).                                       |
| `RECORDSET: <xid>`  | Returns `env.ref(xid)` (the record itself).                                |
| Plain string        | If the field is relational and the string matches `module.xml_id`, it is auto-resolved to the integer id. Otherwise passed through verbatim. |

Two aliases are always present in the registry: `company` (`env.company`) and
`user` (`env.user`). A `save_as` of the same name overrides them.

### Records vs ids

In `create` and `write` the field type is known, so a record landing on a
relational field is coerced for you: `many2one` gets the id, an x2many gets a
`(6, 0, ids)` command. `REC: p` and `REC: p.id` therefore both work on a
`many2one`.

In `form`, `domain`, `args`, and `kwargs` there is no field type to consult,
so nothing is coerced — the value arrives exactly as written. The Form API
wants records, which is why `form` values are written bare (`REC: p`).

The `EVAL:` namespace exposes:

- `self`, `env`, `registry`
- `datetime`, `date`, `time`, `timedelta`, `relativedelta`, `Decimal`
- `len`, `range`, `min`, `max`, `sum`, `abs`, `round`

Anything else triggers a `YamlConfigurationError` at evaluation time.

`EVAL:` remains the right tool for arithmetic, dates, and anything needing
real Python. For the common case of "the id of a record I made earlier",
prefer `REC:`.

## Cache Refresh (opt-in, default off)

With `auto_refresh` on, the target is flushed and its cache invalidated before
each assertion and each `search`. Non-stored compute fields are not
invalidated by Odoo when a dependency such as `state` changes, so without this
an assertion immediately after a state transition reads the pre-transition
value — which is what the manual `invalidate_cache` steps scattered through
the SSI corpus exist to work around.

Opt in with `refresh: true` on a step, `options: {auto_refresh: true}` on a
scenario or file, or `auto_refresh = True` on the test class.

### Why it defaults to off

Invalidating means the field must be re-read from the database. A re-read runs
the record rules that the cached value never had to pass. So enabling this can
turn a passing assertion into an `AccessError` — not because the library
broke, but because the assertion was only ever passing on a stale cache.

This is not hypothetical: enabling it on `ssi_school` turns 0 failures into 15
`AccessError`s out of 79 tests, because those scenarios act
`as_user: base.user_admin` on `school_enrollment` records that Mitchell Admin
has no record-rule access to read.

Treat a new failure after opting in as a real finding about the scenario (or
the module's security rules), not as a library bug.

## Error Messages

When a step fails, the error message has this shape:

```
Error in File: /path/to/scenarios.yaml -> Scenario: 'B2B' -> Step: 'Confirm' (action=call): ValidationError: Cannot confirm: missing line
```

Assertion failures carry the same prefix, and stay `AssertionError`s so
unittest reports them as failures rather than errors:

```
Error in File: /path/to/scenarios.yaml -> Scenario: 'B2B' -> Step: 'Check state' (action=assert): sale.order.state: expected 'sale', got 'draft'
```

A failure inside a `setup:` step reads `-> Setup Step: '...'` instead.

The original exception is chained via `raise ... from e`, so the full
traceback is still visible in pytest output.
