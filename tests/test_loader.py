"""Tests for the YAML loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from odoo_yaml_test.exceptions import YamlConfigurationError
from odoo_yaml_test.loader import load_yaml_file, validate_scenarios_document


class TestLoadYamlFile:
    def test_loads_valid_file(self, fixtures_dir: Path) -> None:
        data = load_yaml_file(fixtures_dir / "valid_basic.yaml")
        assert "scenarios" in data
        assert isinstance(data["scenarios"], list)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(YamlConfigurationError, match="not found"):
            load_yaml_file(tmp_path / "missing.yaml")

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.yaml"
        empty.write_text("")
        with pytest.raises(YamlConfigurationError, match="empty"):
            load_yaml_file(empty)

    def test_non_mapping_top_level_raises(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.yaml"
        bad.write_text("- 1\n- 2\n")
        with pytest.raises(YamlConfigurationError, match="top-level mapping"):
            load_yaml_file(bad)

    def test_invalid_yaml_syntax_raises(self, tmp_path: Path) -> None:
        broken = tmp_path / "broken.yaml"
        broken.write_text("scenarios: [\n  - unclosed")
        with pytest.raises(YamlConfigurationError, match="Failed to parse"):
            load_yaml_file(broken)


class TestValidateScenarios:
    def test_valid_document(self) -> None:
        data = {
            "scenarios": [
                {"name": "s1", "steps": []},
            ]
        }
        result = validate_scenarios_document(data, "test.yaml")
        assert len(result) == 1

    def test_missing_scenarios_key(self) -> None:
        with pytest.raises(YamlConfigurationError, match="missing top-level key"):
            validate_scenarios_document({}, "test.yaml")

    def test_scenarios_not_a_list(self) -> None:
        with pytest.raises(YamlConfigurationError, match="must be a list"):
            validate_scenarios_document({"scenarios": "no"}, "test.yaml")

    def test_scenario_not_mapping(self) -> None:
        with pytest.raises(YamlConfigurationError, match="must be a mapping"):
            validate_scenarios_document({"scenarios": ["s1"]}, "test.yaml")

    def test_scenario_missing_name(self) -> None:
        with pytest.raises(YamlConfigurationError, match="missing 'name'"):
            validate_scenarios_document({"scenarios": [{"steps": []}]}, "test.yaml")

    def test_scenario_missing_steps(self) -> None:
        with pytest.raises(YamlConfigurationError, match="must have a 'steps' list"):
            validate_scenarios_document({"scenarios": [{"name": "s1"}]}, "test.yaml")
