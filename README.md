# odoo-yaml-test

[![PyPI version](https://img.shields.io/pypi/v/odoo-yaml-test.svg)](https://pypi.org/project/odoo-yaml-test/)
[![Python versions](https://img.shields.io/pypi/pyversions/odoo-yaml-test.svg)](https://pypi.org/project/odoo-yaml-test/)
[![License](https://img.shields.io/pypi/l/odoo-yaml-test.svg)](https://github.com/andhitia/odoo-yaml-test/blob/main/LICENSE)

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

```bash
pip install odoo-yaml-test
```

The package targets Python 3.8+ and is primarily tested against
Odoo 14, but it depends only on Odoo's public ORM API and should work
on later versions as well.

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

| Action   | Required                 | Optional                                  | Purpose                                            |
| -------- | ------------------------ | ----------------------------------------- | -------------------------------------------------- |
| `create` | `model`, `values`        | `save_as`, `context`, `as_user`           | Create a record. Values are dynamically resolved.  |
| `write`  | `target`, `values`       | `context`                                 | Update a registered record.                        |
| `call`   | `target`, `method`       | `args`, `kwargs`, `asserts`, `context`    | Invoke a method on a registered record.            |
| `assert` | `target`, `asserts`      |                                           | Validate a registered record's state.              |
| `ref`    | `xml_id`, `save_as`      |                                           | Resolve a single xml_id and store the record.      |
| `search` | `model`, `domain`, `save_as` | `limit`, `order`, `expect_count`      | Search and store the resulting recordset.          |

### Assertion Types

- `value` — direct comparison. Optional `operator`: `equals` (default),
  `not_equals`, `gt`, `gte`, `lt`, `lte`, `in`, `not_in`, `contains`,
  `is_truthy`, `is_falsy`.
- `m2o` — many-to-one. Compares against `env.ref(expected_xml_id).id`.
- `o2m` / `m2m` — relational. `check`: `count` (with `expected_count`),
  `contains_xml_ids`, or `exact_xml_ids` (with `expected_xml_ids`).

### Dynamic Value Prefixes

Inside `values`, `args`, `kwargs`, or `domain`:

- `REF: <xml_id>` — resolves to `env.ref(xml_id).id`.
- `RECORDSET: <xml_id>` — resolves to the record itself (not the id).
- `EVAL: <expression>` — evaluates a Python expression in a restricted
  namespace. See **Security** below.

For relational fields (`many2one`, `one2many`, `many2many`,
`reference`), a bare string matching the `module.xml_id` pattern is
also resolved automatically. For non-relational fields, strings are
passed through verbatim.

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
