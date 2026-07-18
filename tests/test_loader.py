"""Tests for the YAML loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from odoo_yaml_test.case import YamlTransactionCase
from odoo_yaml_test.exceptions import YamlConfigurationError
from odoo_yaml_test.loader import (
    extract_fake_models,
    extract_options,
    extract_setup_steps,
    load_yaml_file,
    validate_scenarios_document,
)


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


class TestExtractFakeModels:
    SHORT = "odoo.addons.mod.tests.fake_models:Thing"

    def test_absent_returns_empty(self) -> None:
        assert extract_fake_models({}, "test.yaml") == {}

    def test_short_form_normalises_to_long_form(self) -> None:
        result = extract_fake_models({"fake_models": [self.SHORT]}, "test.yaml")
        assert result == {
            "classes": [self.SHORT],
            "acl": True,
            "groups": ["base.group_user"],
            "addon": None,
        }

    def test_long_form_keeps_explicit_values(self) -> None:
        result = extract_fake_models(
            {
                "fake_models": {
                    "classes": [self.SHORT],
                    "acl": False,
                    "groups": ["base.group_system"],
                    "addon": "mod",
                }
            },
            "test.yaml",
        )
        assert result["acl"] is False
        assert result["groups"] == ["base.group_system"]
        assert result["addon"] == "mod"

    def test_returned_lists_are_copies(self) -> None:
        """Mutating the result must not write back into the parsed document."""
        classes = [self.SHORT]
        data = {"fake_models": {"classes": classes}}
        result = extract_fake_models(data, "test.yaml")
        result["classes"].append("other")
        assert classes == [self.SHORT]

    def test_scalar_raises(self) -> None:
        with pytest.raises(YamlConfigurationError, match="must be a list of class references"):
            extract_fake_models({"fake_models": "nope"}, "test.yaml")

    def test_unknown_key_raises(self) -> None:
        with pytest.raises(YamlConfigurationError, match="Unknown key"):
            extract_fake_models(
                {"fake_models": {"classes": [self.SHORT], "acls": False}}, "test.yaml"
            )

    @pytest.mark.parametrize("classes", [None, [], "str", {}])
    def test_bad_classes_raises(self, classes) -> None:
        with pytest.raises(YamlConfigurationError, match="must be a non-empty list"):
            extract_fake_models({"fake_models": {"classes": classes}}, "test.yaml")

    def test_non_string_entry_raises(self) -> None:
        with pytest.raises(YamlConfigurationError, match=r"classes\[0\].*must be a string"):
            extract_fake_models({"fake_models": [123]}, "test.yaml")

    @pytest.mark.parametrize("ref", ["no_colon", ":Thing", "module:", "  :  "])
    def test_malformed_reference_raises(self, ref: str) -> None:
        with pytest.raises(YamlConfigurationError, match=r"module\.path:ClassName"):
            extract_fake_models({"fake_models": [ref]}, "test.yaml")

    def test_non_bool_acl_raises(self) -> None:
        with pytest.raises(YamlConfigurationError, match=r"'fake_models\.acl'.*must be a boolean"):
            extract_fake_models(
                {"fake_models": {"classes": [self.SHORT], "acl": "yes"}}, "test.yaml"
            )

    @pytest.mark.parametrize("groups", ["base.group_user", [], {}])
    def test_bad_groups_raises(self, groups) -> None:
        with pytest.raises(YamlConfigurationError, match="must be a non-empty list of xml_id"):
            extract_fake_models(
                {"fake_models": {"classes": [self.SHORT], "groups": groups}}, "test.yaml"
            )

    def test_non_string_group_raises(self) -> None:
        with pytest.raises(YamlConfigurationError, match=r"groups\[0\].*must be a string"):
            extract_fake_models(
                {"fake_models": {"classes": [self.SHORT], "groups": [7]}}, "test.yaml"
            )

    def test_non_string_addon_raises(self) -> None:
        with pytest.raises(YamlConfigurationError, match=r"'fake_models\.addon'.*must be a string"):
            extract_fake_models({"fake_models": {"classes": [self.SHORT], "addon": 1}}, "test.yaml")


class TestRealWorldBackwardCompat:
    """Guard: a real production scenario file must keep loading unchanged.

    Copied verbatim from ssi-account-amortization. 298 such files exist across
    the ssi-* repos; if this one stops validating, they all have.
    """

    def test_real_ssi_scenario_still_validates(self, fixtures_dir) -> None:
        path = fixtures_dir / "real_ssi_amortization.yaml"
        data = load_yaml_file(path)
        scenarios = validate_scenarios_document(data, str(path))
        assert len(scenarios) == 2
        # No setup/options/fake_models block: extraction must degrade to empty,
        # not raise. This is what keeps the 298 files in the wild loading.
        assert extract_setup_steps(data, str(path)) == []
        assert extract_options(data, str(path)) == {}
        assert extract_fake_models(data, str(path)) == {}

    def test_every_action_used_in_the_wild_has_a_handler(self, fixtures_dir) -> None:
        path = fixtures_dir / "real_ssi_amortization.yaml"
        scenarios = validate_scenarios_document(load_yaml_file(path), str(path))
        actions = {step["action"] for scenario in scenarios for step in scenario["steps"]}
        for action in actions:
            assert hasattr(YamlTransactionCase, f"_action_{action}"), action
