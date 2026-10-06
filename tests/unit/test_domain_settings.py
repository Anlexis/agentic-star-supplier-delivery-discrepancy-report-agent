# The runtime tuning block — every declared value is validated at compile time
# and reaches the domain nodes through seeded state.
#
# Deterministic — no model, no network.

import pytest
from framework.errors import ConfigError

from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.graph.graph import RetC2158Agent
from src.services.service import DEFAULT_SETTINGS, parse_domain_settings, settings_from_state

_WEIGHTS = dict(DEFAULT_SETTINGS["kpi_weights"])


class TestParseDomainSettings:
    def test_absent_block_takes_the_shipped_defaults(self):
        assert parse_domain_settings(None) == DEFAULT_SETTINGS
        assert parse_domain_settings({}) == DEFAULT_SETTINGS

    def test_declared_values_are_normalised(self):
        parsed = parse_domain_settings({"late_delivery_tolerance_days": 2.0, "price_tolerance_pct": 0.05})
        assert parsed["late_delivery_tolerance_days"] == 2
        assert parsed["price_tolerance_pct"] == 0.05
        assert parsed["kpi_weights"] == _WEIGHTS

    @pytest.mark.parametrize(
        "raw, key",
        [
            ({"data_source": "erp"}, "data_source"),
            ({"unknown": 1}, "unknown"),
            ({"late_delivery_tolerance_days": -1}, "late_delivery_tolerance_days"),
            ({"late_delivery_tolerance_days": 1.5}, "late_delivery_tolerance_days"),
            ({"late_delivery_tolerance_days": float("nan")}, "late_delivery_tolerance_days"),
            ({"late_delivery_tolerance_days": "3"}, "late_delivery_tolerance_days"),
            ({"price_tolerance_pct": float("inf")}, "price_tolerance_pct"),
            ({"price_tolerance_pct": 1.5}, "price_tolerance_pct"),
            ({"price_tolerance_pct": True}, "price_tolerance_pct"),
            ({"kpi_weights": {"otd_pct": 1.0}}, "kpi_weights"),
            ({"kpi_weights": dict(_WEIGHTS, otd_pct=float("nan"))}, "kpi_weights.otd_pct"),
            ({"kpi_weights": dict(_WEIGHTS, otd_pct=0.5)}, "kpi_weights must sum"),
            ({"kpi_weights": "equal"}, "kpi_weights"),
            ("mock", "must be a mapping"),
        ],
    )
    def test_malformed_value_is_refused_naming_the_key(self, raw, key):
        with pytest.raises(ValueError) as raised:
            parse_domain_settings(raw)
        assert key in str(raised.value)

    def test_settings_from_state_falls_back_to_defaults(self):
        assert settings_from_state({}) == DEFAULT_SETTINGS
        assert (
            settings_from_state({"domain_settings": {"late_delivery_tolerance_days": 4}})[
                "late_delivery_tolerance_days"
            ]
            == 4
        )
        assert settings_from_state({"domain_settings": {"late_delivery_tolerance_days": -4}}) == DEFAULT_SETTINGS


class TestSettingsReachBothGraphs:
    def test_outer_graph_refuses_a_malformed_block_at_compile(self):
        agent = RetC2158Agent(config={"ret_c2_158": {"price_tolerance_pct": 2.0}})
        with pytest.raises(ConfigError):
            agent.compile()

    def test_outer_graph_seeds_the_validated_block(self):
        agent = RetC2158Agent(config={"ret_c2_158": {"late_delivery_tolerance_days": 7}})
        seeded = agent._extra_initial_state()["domain_settings"]
        assert seeded["late_delivery_tolerance_days"] == 7
        assert seeded["kpi_weights"] == _WEIGHTS

    def test_main_slot_forwards_the_block_to_the_inner_graph(self):
        agent = RetC2158Agent(config={"ret_c2_158": {"price_tolerance_pct": 0.1}})
        agent.compile()
        inner = agent._nodes["main"].get_subgraph()
        assert inner.config["ret_c2_158"]["price_tolerance_pct"] == 0.1
        inner._validate_config()
        assert inner._extra_initial_state()["domain_settings"]["price_tolerance_pct"] == 0.1

    def test_inner_graph_refuses_a_malformed_block_at_compile(self):
        with pytest.raises(ConfigError):
            DomainWorkflowGraph(config={"ret_c2_158": {"data_source": "erp"}}).compile()

    def test_shipped_config_file_parses(self):
        from pathlib import Path

        import yaml

        loaded = yaml.safe_load((Path(__file__).resolve().parents[2] / "config" / "config.yaml").read_text())
        assert parse_domain_settings(loaded["ret_c2_158"]) == DEFAULT_SETTINGS
        assert loaded["max_retry"] == 3
        assert loaded["timeout_s"] == 30
