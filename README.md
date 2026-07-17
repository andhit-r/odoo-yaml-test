# odoo-yaml-test

[![CI](https://github.com/andhit-r/odoo-yaml-test/actions/workflows/ci.yml/badge.svg)](https://github.com/andhit-r/odoo-yaml-test/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/andhit-r/odoo-yaml-test)](https://github.com/andhit-r/odoo-yaml-test/releases)
[![License](https://img.shields.io/github/license/andhit-r/odoo-yaml-test)](https://github.com/andhit-r/odoo-yaml-test/blob/master/LICENSE)

Data-driven unit testing framework for Odoo, powered by YAML scenarios.

`odoo-yaml-test` provides a single base class — `YamlTransactionCase` —
that lets you express test fixtures and behavioural checks in YAML
instead of repetitive Python boilerplate. It is built specifically to
**avoid the pitfalls** that led Odoo to deprecate its legacy
`test/*.yml` mechanism after Odoo 11:

- The YAML schema is **strictly declarative**. No control-flow keywords.
- Every step runs inside a contextual error wrapper that points to the
  exact file, scenario, and step that failed.
- Each scenario runs inside its own `subTest`, so a failure in one
  scenario never blocks the next.
- Only Odoo's **public ORM API** is used (`create`, `write`, `search`,
  `env.ref`, etc.). No private attribute poking, no SQL, no monkey
  patching.
- The per-scenario record registry is reset between scenarios — no
  cross-scenario state leakage.

## Installation

The package is not on PyPI; it is installed straight from GitHub.

```bash
# latest
pip install "git+https://github.com/andhit-r/odoo-yaml-test.git@19.0#egg=odoo-yaml-test"

# pinned to a release
pip install "git+https://github.com/andhit-r/odoo-yaml-test.git@19.0-v0.4.0#egg=odoo-yaml-test"
```

Released versions are listed on the
[Releases page](https://github.com/andhit-r/odoo-yaml-test/releases).

The package targets Python 3.10+. This library targets **Odoo 19.0** and is tested
only against it — including one CI job that runs against a real Odoo 19 registry. The
public ORM API this library uses is *not* stable across series, so each series gets its
own branch: `flush()` and `invalidate_cache()` were removed in Odoo 17, and this branch
calls 19's `env.flush_all()` / `invalidate_recordset()` directly. For Odoo 14.0 use the
`master` branch instead. See `BRANCHING.md`.

## Quickstart

Create a YAML file next to your Odoo test module:

```yaml
# addons/my_module/tests/test_data.yaml
scenarios:
  - name: "B2B Sales Scenario"
    steps:
      - step: "Create Sales Order"
        action: "create"
        model: "sale.order"
        save_as: "so_1"
        values:
          partner_id: "REF: base.res_partner_1"
          date_order: "EVAL: datetime.now()"
          note: "Initial order"

      - step: "Add order line"
        action: "create"
        model: "sale.order.line"
        values:
          order_id: "EVAL: registry['so_1'].id"
          product_id: "REF: product.product_product_4"
          product_uom_qty: 10

      - step: "Confirm order"
        action: "call"
        target: "so_1"
        method: "action_confirm"
        asserts:
          state:
            type: "value"
            expected: "sale"

      - step: "Validate relations"
        action: "assert"
        target: "so_1"
        asserts:
          partner_id:
            type: "m2o"
            expected_xml_id: "base.res_partner_1"
          order_line:
            type: "o2m"
            check: "count"
            expected_count: 1
```

Then write a thin Python test class that points to it:

```python
# addons/my_module/tests/test_sale.py
from odoo_yaml_test import YamlTransactionCase


class TestSaleOrderYAML(YamlTransactionCase):
    def test_b2b_scenario(self):
        # The YAML file is auto-discovered relative to this test file.
        self.run_yaml_scenario("test_data.yaml")
```

## Action Reference

Every action also accepts `as_user`, `context`, and `expect_error`.

| Action   | Required                     | Optional                               | Purpose                                           |
| -------- | ---------------------------- | -------------------------------------- | ------------------------------------------------- |
| `create` | `model`, `values`            | `save_as`                              | Create a record. Values are dynamically resolved. |
| `write`  | `target`, `values`           |                                        | Update a registered record.                       |
| `call`   | `target`, `method`           | `args`, `kwargs`, `asserts`            | Invoke a method on a registered record.           |
| `assert` | `target`, `asserts`          | `refresh`                              | Validate a registered record's state.             |
| `unlink` | `target`                     |                                        | Delete a registered record.                       |
| `ref`    | `xml_id`, `save_as`          |                                        | Resolve a single xml_id and store the record.     |
| `search` | `model`, `domain`, `save_as` | `limit`, `order`, `expect_count`       | Search and store the resulting recordset.         |
| `wizard` | `model`, `target`            | `values`, `method`, `save_as`, `asserts` | Run a transient wizard against a record.        |
| `form`   | `model` **or** `target`      | `values` \| `ops`, `asserts`, `save`, `save_as` | Drive the Form API — the only way to test onchange. |

### Assertion Types

- `value` — direct comparison. Optional `operator`: `equals` (default),
  `not_equals`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `contains`,
  `is_truthy`, `is_falsy`.
- `m2o` — many-to-one. `expected` (a record, id, or any prefix) or
  `expected_xml_id`.
- `o2m` / `m2m` — relational. `check`: `count` (with `expected_count`),
  `contains` / `exact` (with `expected_records`), or the xml_id-only
  `contains_xml_ids` / `exact_xml_ids` (with `expected_xml_ids`).

Field names may be dotted to read across a relation:

```yaml
asserts:
  partner_id.name:
    type: "value"
    expected: "Acme"
```

### Dynamic Value Prefixes

Usable in `values`, `args`, `kwargs`, `domain`, `context`, and inside
`asserts`:

- `REC: <alias>[.<attr>…]` — a record saved earlier in this scenario.
  `REC: so_1` is the record, `REC: so_1.id` its id, `REC: so_1.partner_id.name`
  walks the relation. No calls or operators — use `EVAL:` for those.
- `REF: <xml_id>` — resolves to `env.ref(xml_id).id`.
- `RECORDSET: <xml_id>` — resolves to the record itself (not the id).
- `EVAL: <expression>` — evaluates a Python expression in a restricted
  namespace. See **Security** below.

Two aliases are always in the registry: `company` (`env.company`) and `user`
(`env.user`). A `save_as` of the same name overrides them.

In `create` and `write`, a record landing on a relational field is coerced to
what the ORM expects — an id for `many2one`, a `(6, 0, ids)` command for
x2many. In `form`, values are passed as *records*, because that is what the
Form API wants.

For relational fields (`many2one`, `one2many`, `many2many`,
`reference`), a bare string matching the `module.xml_id` pattern is
also resolved automatically. For non-relational fields, strings are
passed through verbatim.

## Testing the Unhappy Path

```yaml
- step: "Confirming without approval must be refused"
  action: "call"
  target: "amortization"
  method: "action_confirm"
  as_user: "some_non_admin_user"     # a registry alias, or an xml_id
  expect_error:
    type: "UserError"
    message_contains: "not allowed"
```

The step runs inside a cursor savepoint, so an expected `ValidationError`
rolls back cleanly instead of poisoning the rest of the transaction.

## Onchange

Onchange methods do not run through `create()` or `write()` — only through
Odoo's `Form` API, which the `form` action drives:

```yaml
- step: "Changing the partner clears the type"
  action: "form"
  model: "sale.order"
  save_as: "so"
  values:                       # applied in order; each may fire an onchange
    name: "SO001"
    partner_id: "REC: customer" # a RECORD, not an id
  asserts:                      # checked on the pending form, before save()
    type_id:
      type: "value"
      operator: "is_falsy"
```

When assignment order matters, a field must be set twice to re-fire an
onchange, or x2many lines are built inline, use `ops:` instead of `values:`:

```yaml
  ops:
    - set: {partner_id: "REC: customer"}
    - set: {partner_id: "REC: customer"}   # re-fire — a mapping can't say this
    - new:
        field: "order_line"
        values:
          product_id: "REC: product"
          product_uom_qty: 2
    - assert: {amount_total: {type: "value", operator: "gt", expected: 0}}
```

## Shared Fixtures

A top-level `setup:` block runs before *each* scenario, against a freshly
reset registry — so scenarios share fixtures without sharing state:

```yaml
setup:
  steps:
    - step: "Create journal"
      action: "create"
      model: "account.journal"
      save_as: "journal"
      values: {name: "Test", code: "TST", type: "general"}

scenarios:
  - name: "Workflow"
    steps: [...]          # `journal` is already in the registry
```

## Cache Refresh (opt-in)

Non-stored compute fields are not invalidated by Odoo when a dependency like
`state` changes, so an assertion right after a state transition can read a
stale value. Turn on `auto_refresh` and the record is flushed and its cache
invalidated before each assertion:

```yaml
options:
  auto_refresh: true      # per file
scenarios:
  - name: "..."
    options: {auto_refresh: true}    # or per scenario
    steps:
      - step: "..."
        action: "assert"
        refresh: true                 # or per step
```

or `auto_refresh = True` on the test class.

**It is off by default, deliberately.** Invalidating forces the field to be
re-read from the database, and a re-read runs the record rules that a cached
value never had to pass. Switching it on therefore surfaces assertions that
only ever passed because nobody re-read the value — in one real SSI module,
15 of 79 passing tests turn into `AccessError`, because the scenarios act
`as_user` on records that user cannot actually read. Those tests are wrong,
but fixing them is a decision for the module owner, not a side effect of
upgrading this library.

## Security: the `EVAL:` Sandbox

`EVAL:` is evaluated by `safe_eval`, which:

1. Tries `ast.literal_eval` first (pure literals never need anything
   else).
2. Otherwise compiles the expression with empty `__builtins__` and a
   whitelisted globals dict.
3. Walks the AST and rejects imports, lambdas, function/class
   definitions, dunder attribute access, and known-dangerous builtins
   (`eval`, `exec`, `compile`, `__import__`, `getattr`, `open`, …).

The whitelist covers `datetime`, `date`, `time`, `timedelta`,
`relativedelta`, `Decimal`, plus a few safe math builtins, and exposes
`self`, `env`, and `registry` to the expression.

**This is not a sandbox for untrusted input.** A YAML author can still
mutate database state via `self` or `env`. Treat YAML files as trusted
test code, exactly as you would treat the Python file that loads them.

## Contributing

```bash
git clone https://github.com/andhitia/odoo-yaml-test.git
cd odoo-yaml-test
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
pytest
```

Issues and pull requests are welcome on
[GitHub](https://github.com/andhitia/odoo-yaml-test/issues).

## License

Apache-2.0. See [LICENSE](LICENSE).
