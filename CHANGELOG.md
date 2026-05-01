# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
