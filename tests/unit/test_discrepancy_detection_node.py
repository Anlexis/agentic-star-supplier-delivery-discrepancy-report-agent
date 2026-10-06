# RET-C2-158 — Unit Tests: DiscrepancyDetectionNode (BL-01, BL-03)
#
# BL-01: 3-way match correct for mixed on-time/late/shortfall/price-mismatch fixture
# BL-03: discrepancy flags generated for known-bad data; no false positives for clean
#
# Plus: the tolerances arrive through seeded state, every flag carries its
# measure as a structured field, and no unit price is rendered.

import pytest
from framework.schemas.agent_status import AgentStatus

from src.nodes.discrepancy_detection_node import DiscrepancyDetectionNode


def _fixture(period="2026-01"):
    """Two POs: PO-001 (on-time, short by 5, matching invoice), PO-002 (late, full, price mismatch)."""
    return {
        "purchase_orders": [
            {
                "po_id": f"PO-{period}-001",
                "supplier_id": "SUP-001",
                "line_item": "WIDGET-A",
                "qty_ordered": 100,
                "unit_price": 5.00,
                "planned_delivery_date": f"{period}-15",
            },
            {
                "po_id": f"PO-{period}-002",
                "supplier_id": "SUP-001",
                "line_item": "WIDGET-B",
                "qty_ordered": 50,
                "unit_price": 12.00,
                "planned_delivery_date": f"{period}-10",
            },
        ],
        "delivery_records": [
            {"po_id": f"PO-{period}-001", "qty_received": 95, "actual_delivery_date": f"{period}-15"},
            {"po_id": f"PO-{period}-002", "qty_received": 50, "actual_delivery_date": f"{period}-25"},
        ],
        "invoice_records": [
            {"po_id": f"PO-{period}-001", "invoiced_price": 5.00, "invoiced_qty": 95},
            {"po_id": f"PO-{period}-002", "invoiced_price": 13.20, "invoiced_qty": 50},
        ],
        "defect_returns": [],
    }


def _flags(state):
    result = DiscrepancyDetectionNode().execute(state)
    assert result["status"] == AgentStatus.SUCCESS.value
    return result["discrepancy_flags"], result


class TestBL01ThreeWayMatchCorrect:
    def test_matched_triples_count(self):
        _, result = _flags(_fixture())
        assert len(result["matched_triples"]) == 2

    def test_qty_shortfall_flagged_for_po_001(self):
        flags, _ = _flags(_fixture())
        shortfall = [f for f in flags if f["flag_type"] == "qty_shortfall"]
        assert len(shortfall) == 1
        assert shortfall[0]["po_id"] == "PO-2026-01-001"
        assert shortfall[0]["shortfall"] == 5
        assert shortfall[0]["severity"] == "MEDIUM"

    def test_late_delivery_flagged_for_po_002(self):
        flags, _ = _flags(_fixture())
        late = [f for f in flags if f["flag_type"] == "late_delivery"]
        assert len(late) == 1
        assert late[0]["days_late"] == 15
        assert late[0]["severity"] == "HIGH"

    def test_invoice_mismatch_flagged_for_po_002(self):
        flags, _ = _flags(_fixture())
        mismatch = [f for f in flags if f["flag_type"] == "invoice_mismatch"]
        assert len(mismatch) == 1
        assert mismatch[0]["price_deviation_pct"] == 10.0
        assert mismatch[0]["severity"] == "MEDIUM"

    def test_unit_prices_are_not_rendered_into_the_detail(self):
        flags, _ = _flags(_fixture())
        mismatch = [f for f in flags if f["flag_type"] == "invoice_mismatch"][0]
        assert "13.2" not in mismatch["detail"]
        assert "12.0" not in mismatch["detail"]
        assert "10.0%" in mismatch["detail"]


class TestBL03NoFalsePositives:
    def test_no_flags_for_clean_records(self):
        clean = {
            "purchase_orders": [
                {
                    "po_id": "PO-CLEAN-001",
                    "supplier_id": "SUP-OK",
                    "line_item": "GOOD-PART",
                    "qty_ordered": 100,
                    "unit_price": 10.00,
                    "planned_delivery_date": "2026-01-15",
                }
            ],
            "delivery_records": [{"po_id": "PO-CLEAN-001", "qty_received": 100, "actual_delivery_date": "2026-01-15"}],
            "invoice_records": [{"po_id": "PO-CLEAN-001", "invoiced_price": 10.00, "invoiced_qty": 100}],
            "defect_returns": [],
        }
        flags, _ = _flags(clean)
        assert flags == []

    def test_early_delivery_is_not_late(self):
        state = _fixture()
        state["delivery_records"][1]["actual_delivery_date"] = "2026-01-05"
        flags, _ = _flags(state)
        assert not [f for f in flags if f["flag_type"] == "late_delivery"]


class TestDefectReturns:
    def test_defect_flag_carries_the_structured_quantity(self):
        state = _fixture()
        state["defect_returns"] = [{"po_id": "PO-2026-01-001", "qty_defective": 8, "return_reason": "cosmetic_defect"}]
        flags, _ = _flags(state)
        defect = [f for f in flags if f["flag_type"] == "defect_return"][0]
        assert defect["qty_defective"] == 8
        assert defect["severity"] == "HIGH"  # 8 of 95 received is above 5%
        assert "cosmetic_defect" in defect["detail"]

    def test_zero_quantity_return_raises_no_flag(self):
        state = _fixture()
        state["defect_returns"] = [{"po_id": "PO-2026-01-001", "qty_defective": 0}]
        flags, _ = _flags(state)
        assert not [f for f in flags if f["flag_type"] == "defect_return"]


class TestTolerancesAreLive:
    def test_late_tolerance_from_seeded_settings_absorbs_the_delay(self):
        flags, _ = _flags(dict(_fixture(), domain_settings={"late_delivery_tolerance_days": 20}))
        assert not [f for f in flags if f["flag_type"] == "late_delivery"]

    def test_price_tolerance_from_seeded_settings_absorbs_the_deviation(self):
        flags, _ = _flags(dict(_fixture(), domain_settings={"price_tolerance_pct": 0.15}))
        assert not [f for f in flags if f["flag_type"] == "invoice_mismatch"]

    def test_a_malformed_seeded_block_falls_back_to_the_defaults(self):
        flags, _ = _flags(dict(_fixture(), domain_settings={"late_delivery_tolerance_days": -1}))
        assert [f for f in flags if f["flag_type"] == "late_delivery"]


class TestFailClosed:
    def test_an_earlier_error_is_carried_forward(self):
        assert DiscrepancyDetectionNode().execute(dict(_fixture(), status=AgentStatus.ERROR.value)) == {}

    @pytest.mark.parametrize("state", [{}, {"purchase_orders": None}])
    def test_empty_input_yields_no_flags(self, state):
        flags, result = _flags(state)
        assert flags == [] and result["matched_triples"] == []
