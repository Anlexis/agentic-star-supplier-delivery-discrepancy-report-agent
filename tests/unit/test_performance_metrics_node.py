# RET-C2-158 — Unit Tests: PerformanceMetricsNode (BL-02, BL-05)
#
# BL-02: OTD%, Fill Rate%, OTIF% correct for known fixture
# BL-05: supplier ranking sorted descending by composite_score
#
# Plus: the composite weights arrive through seeded state, and defect
# quantities are read from the structured flag field.

from framework.schemas.agent_status import AgentStatus

from src.nodes.performance_metrics_node import PerformanceMetricsNode


def _two_delivery_fixture():
    """PO-001 on-time and full; PO-002 late and full → OTD 50, fill 100, OTIF 50."""
    triples = [
        {"po_id": "PO-2026-001", "supplier_id": "SUP-001", "qty_ordered": 100, "qty_received": 100},
        {"po_id": "PO-2026-002", "supplier_id": "SUP-001", "qty_ordered": 50, "qty_received": 50},
    ]
    flags = [
        {
            "supplier_id": "SUP-001",
            "po_id": "PO-2026-002",
            "flag_type": "late_delivery",
            "severity": "HIGH",
            "days_late": 15,
        }
    ]
    return {"matched_triples": triples, "discrepancy_flags": flags}


def _kpi(state, supplier="SUP-001"):
    result = PerformanceMetricsNode().execute(state)
    assert result["status"] == AgentStatus.SUCCESS.value
    return result["supplier_kpis"][supplier], result


class TestBL02KpiCalculations:
    def test_otd_fill_and_otif(self):
        kpi, _ = _kpi(_two_delivery_fixture())
        assert kpi["otd_pct"] == 50.0
        assert kpi["fill_rate"] == 100.0
        assert kpi["otif_pct"] == 50.0
        assert kpi["invoice_accuracy_pct"] == 100.0

    def test_otif_le_min_otd_fill_rate(self):
        kpi, _ = _kpi(_two_delivery_fixture())
        assert kpi["otif_pct"] <= min(kpi["otd_pct"], kpi["fill_rate"]) + 0.01

    def test_division_by_zero_guard(self):
        result = PerformanceMetricsNode().execute({"matched_triples": [], "discrepancy_flags": []})
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["supplier_kpis"] == {}
        assert result["supplier_rankings"] == []

    def test_composite_uses_the_default_weights(self):
        kpi, _ = _kpi(_two_delivery_fixture())
        # 0.25*50 + 0.25*100 + 0.20*50 + 0.15*100 + 0.15*100 = 77.5
        assert kpi["composite_score"] == 77.5

    def test_defect_quantity_is_read_from_the_structured_flag(self):
        state = _two_delivery_fixture()
        state["discrepancy_flags"].append(
            {
                "supplier_id": "SUP-001",
                "po_id": "PO-2026-001",
                "flag_type": "defect_return",
                "severity": "HIGH",
                "qty_defective": 15,
                "detail": "unrelated text 999",
            }
        )
        kpi, _ = _kpi(state)
        assert kpi["defect_pct"] == 10.0  # 15 of 150 received

    def test_invoice_mismatch_lowers_invoice_accuracy(self):
        state = _two_delivery_fixture()
        state["discrepancy_flags"].append(
            {"supplier_id": "SUP-001", "po_id": "PO-2026-001", "flag_type": "invoice_mismatch", "severity": "MEDIUM"}
        )
        kpi, _ = _kpi(state)
        assert kpi["invoice_accuracy_pct"] == 50.0


class TestBL05SupplierRankingDescending:
    def test_two_supplier_ranking_order(self):
        triples = [
            {"po_id": "PA-001", "supplier_id": "SUP-A", "qty_ordered": 100, "qty_received": 100},
            {"po_id": "PB-001", "supplier_id": "SUP-B", "qty_ordered": 100, "qty_received": 70},
        ]
        flags = [{"supplier_id": "SUP-B", "po_id": "PB-001", "flag_type": "late_delivery", "severity": "HIGH"}]
        result = PerformanceMetricsNode().execute({"matched_triples": triples, "discrepancy_flags": flags})
        rankings = result["supplier_rankings"]
        assert [r["supplier_id"] for r in rankings] == ["SUP-A", "SUP-B"]
        assert [r["rank"] for r in rankings] == [1, 2]

    def test_rankings_sorted_descending(self):
        _, result = _kpi(_two_delivery_fixture())
        scores = [r["composite_score"] for r in result["supplier_rankings"]]
        assert scores == sorted(scores, reverse=True)

    def test_ties_break_on_the_identifier_for_a_stable_report(self):
        triples = [
            {"po_id": "P1", "supplier_id": "SUP-Z", "qty_ordered": 1, "qty_received": 1},
            {"po_id": "P2", "supplier_id": "SUP-A", "qty_ordered": 1, "qty_received": 1},
        ]
        result = PerformanceMetricsNode().execute({"matched_triples": triples, "discrepancy_flags": []})
        assert [r["supplier_id"] for r in result["supplier_rankings"]] == ["SUP-A", "SUP-Z"]


class TestWeightsAreLive:
    def test_seeded_weights_change_the_composite(self):
        weights = {"otd_pct": 1.0, "fill_rate": 0.0, "otif_pct": 0.0, "defect_pct": 0.0, "invoice_accuracy_pct": 0.0}
        kpi, _ = _kpi(dict(_two_delivery_fixture(), domain_settings={"kpi_weights": weights}))
        assert kpi["composite_score"] == 50.0

    def test_an_earlier_error_is_carried_forward(self):
        assert PerformanceMetricsNode().execute(dict(_two_delivery_fixture(), status=AgentStatus.ERROR.value)) == {}
