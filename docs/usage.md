# YAML Schema Reference

This document is the authoritative reference for the YAML format
consumed by `YamlTransactionCase`. For a quick tour, see the
project [README](../README.md).

## Top-Level Structure

```yaml
scenarios:
  - name: "Human-readable scenario name"
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

### `m2o`

Compares a many-to-one field's id with `env.ref(expected_xml_id).id`.

```yaml
asserts:
  partner_id:
    type: "m2o"
    expected_xml_id: "base.res_partner_1"
```

### `o2m` and `m2m`

Three `check` modes:

```yaml
# Count check
asserts:
  order_line:
    type: "o2m"
    check: "count"
    expected_count: 3

# Subset check (all listed xml_ids must be present)
asserts:
  tag_ids:
    type: "m2m"
    check: "contains_xml_ids"
    expected_xml_ids:
      - "base.tag_a"
      - "base.tag_b"

# Exact set check (order-insensitive equality)
asserts:
  tag_ids:
    type: "m2m"
    check: "exact_xml_ids"
    expected_xml_ids:
      - "base.tag_a"
      - "base.tag_b"
```

## Dynamic Value Prefixes

Inside `values`, `args`, `kwargs`, or `domain`:

| Prefix         | Behaviour                                                                  |
| -------------- | -------------------------------------------------------------------------- |
| `EVAL: <expr>` | Evaluates `<expr>` in a restricted namespace (see Security in README).     |
| `REF: <xid>`   | Returns `env.ref(xid).id` (integer).                                       |
| `RECORDSET: <xid>` | Returns `env.ref(xid)` (the record itself).                            |
| Plain string   | If the field is relational and the string matches `module.xml_id`, it is auto-resolved to the integer id. Otherwise passed through verbatim. |

The `EVAL:` namespace exposes:

- `self`, `env`, `registry`
- `datetime`, `date`, `time`, `timedelta`, `relativedelta`, `Decimal`
- `len`, `range`, `min`, `max`, `sum`, `abs`, `round`

Anything else triggers a `YamlConfigurationError` at evaluation time.

## Error Messages

When a step fails, the error message has this shape:

```
Error in File: /path/to/scenarios.yaml -> Scenario: 'B2B' -> Step: 'Confirm' (action=call): ValidationError: Cannot confirm: missing line
```

The original exception is chained via `raise ... from e`, so the full
traceback is still visible in pytest output.
