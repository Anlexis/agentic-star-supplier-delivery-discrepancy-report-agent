# RET-C2-158 — Unit Tests: ReportGenerationNode (BL-04, TC-09)
#
# BL-04: report_markdown non-empty and contains the section headers
# TC-09: report_markdown non-empty on valid KPI data
#
# The scorecard is rendered deterministically; no model is involved. Every
# rendered value is an inert identifier, a validated date or a computed number.

from framework.schemas.agent_status import AgentStatus

from src.nodes.report_generation_node import MAX_ALERT_ROWS, ReportGenerationNode


def _kpis():
    return {
        "SUP-001": {
            "otd_pct": 95.0,
            "fill_rate": 98.0,
            "otif_pct": 93.0,
            "defect_pct": 1.5,
            "invoice_accuracy_pct": 99.0,
            "composite_score": 87.5,
        },
        "SUP-002": {
            "otd_pct": 75.0,
            "fill_rate": 80.0,
            "otif_pct": 65.0,
            "defect_pct": 5.0,
            "invoice_accuracy_pct": 90.0,
            "composite_score": 72.0,
        },
    }


def _rankings():
    return [
        {"rank": 1, "supplier_id": "SUP-001", "composite_score": 87.5},
        {"rank": 2, "supplier_id": "SUP-002", "composite_score": 72.0},
    ]


def _flags():
    return [
        {
            "supplier_id": "SUP-002",
            "po_id": "PO-002",
            "flag_type": "late_delivery",
            "severity": "HIGH",
            "detail": "Planned 2026-01-10; actual 2026-01-25 (15 day(s) late)",
        }
    ]


def _execute(**overrides):
    state = {
        "supplier_kpis": _kpis(),
        "supplier_rankings": _rankings(),
        "discrepancy_flags": _flags(),
        "matched_triples": [{"po_id": "PO-001"}, {"po_id": "PO-002"}],
        "validated_input": {"period": "2026-01"},
        "record_source": "caller",
    }
    state.update(overrides)
    return ReportGenerationNode().execute(state)


class TestBL04ReportMarkdownContent:
    def test_report_markdown_non_empty_and_sectioned(self):
        result = _execute()
        assert result["status"] == AgentStatus.SUCCESS.value
        report = result["report_markdown"]
        for heading in (
            "## Executive Summary",
            "## KPI Summary Table",
            "## Supplier Rankings",
            "## Alert Flags",
            "## Action Recommendations",
        ):
            assert heading in report

    def test_report_metadata_fully_populated(self):
        metadata = _execute()["report_metadata"]
        assert metadata["period"] == "2026-01"
        assert metadata["generated_at"].endswith("Z")
        assert metadata["supplier_count"] == 2
        assert metadata["alert_count"] == 1
        assert metadata["record_source"] == "caller"

    def test_zero_supplier_edge_case_no_crash(self):
        result = _execute(supplier_kpis={}, supplier_rankings=[], discrepancy_flags=[], matched_triples=[])
        assert result["status"] == AgentStatus.SUCCESS.value
        assert "No purchase orders matched the request" in result["report_markdown"]
        assert "No suppliers to rank." in result["report_markdown"]

    def test_result_equals_report_markdown(self):
        result = _execute()
        assert result["result"] == result["report_markdown"]

    def test_data_source_is_stated_in_the_report(self):
        assert "2 purchase order(s) supplied with the request" in _execute()["report_markdown"]
        assert "the built-in sample dataset" in _execute(record_source="baseline")["report_markdown"]

    def test_high_alerts_are_rendered_and_capped(self):
        many = [dict(_flags()[0], po_id=f"PO-{i:03d}") for i in range(MAX_ALERT_ROWS + 3)]
        report = _execute(discrepancy_flags=many)["report_markdown"]
        assert report.count("**[LATE_DELIVERY]**") == MAX_ALERT_ROWS
        assert "and 3 further HIGH-severity flag(s)" in report

    def test_recommendations_follow_the_kpis(self):
        report = _execute()["report_markdown"]
        assert "Escalate the late-delivery pattern for SUP-002 (OTD 75.0% is below 90%)." in report
        assert "Reconcile invoice pricing with SUP-001 (invoice accuracy 99.0%)." in report
        clean = _execute(
            supplier_kpis={
                "SUP-9": {
                    "otd_pct": 100.0,
                    "fill_rate": 100.0,
                    "otif_pct": 100.0,
                    "defect_pct": 0.0,
                    "invoice_accuracy_pct": 100.0,
                    "composite_score": 100.0,
                }
            },
            discrepancy_flags=[],
        )
        assert "- No action items this period." in clean["report_markdown"]

    def test_period_comes_from_the_inner_user_input_when_seeded_that_way(self):
        result = _execute(validated_input=None, user_input={"period": "2026-03"})
        assert "Scorecard — 2026-03" in result["report_markdown"]


class TestTC09ReportMarkdownNonEmpty:
    def test_valid_kpis_produce_non_empty_report(self):
        result = _execute(validated_input={"period": "2026-06"}, discrepancy_flags=[])
        assert result["status"] == AgentStatus.SUCCESS.value
        assert isinstance(result["report_markdown"], str) and result["report_markdown"]

    def test_an_earlier_error_is_carried_forward(self):
        assert _execute(status=AgentStatus.ERROR.value) == {}
