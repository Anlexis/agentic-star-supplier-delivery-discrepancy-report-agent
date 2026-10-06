# RET-C2-158 — Unit Tests: DataIngestionNode
#
# The node publishes the caller's validated records when the request carries
# them, the built-in sample dataset when it does not and the deployment allows
# it, and refuses otherwise. The runtime tuning arrives through seeded state
# (`domain_settings`), never through a config argument.

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.data_ingestion_node import DataIngestionNode

_PO = {"po_id": "PO-1", "supplier_id": "SUP-1", "qty_ordered": 100, "planned_delivery_date": "2026-01-15"}


def _request(period="2026-01", supplier_filter=None, records=None):
    return {
        "period": period,
        "supplier_filter": supplier_filter,
        "output_format": "markdown",
        "records": records,
        "record_source": "caller" if records is not None else "baseline",
    }


def _execute(request, **state):
    payload = {"validated_input": request}
    payload.update(state)
    return DataIngestionNode().execute(payload)


class TestTrustLevel:
    def test_matches_the_manifest(self):
        assert DataIngestionNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL


class TestBaselinePath:
    def test_sample_dataset_populates_all_four_lists(self):
        result = _execute(_request())
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_source"] == "baseline"
        for field in ("purchase_orders", "delivery_records", "defect_returns", "invoice_records"):
            assert isinstance(result[field], list) and result[field]

    def test_sample_dataset_is_period_specific(self):
        result = _execute(_request(period="2025-11"))
        assert result["purchase_orders"][0]["po_id"] == "PO-2025-11-001"

    def test_supplier_filter_restricts_the_sample(self):
        result = _execute(_request(supplier_filter=["SUP-001"]))
        assert {r["supplier_id"] for r in result["purchase_orders"]} == {"SUP-001"}
        assert all(r["po_id"].endswith(("-001", "-002")) for r in result["delivery_records"])

    def test_unknown_filter_yields_empty_lists_without_error(self):
        result = _execute(_request(supplier_filter=["NONEXISTENT"]))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["purchase_orders"] == []
        assert result["delivery_records"] == []

    def test_caller_only_deployment_refuses_the_baseline(self):
        result = _execute(_request(), domain_settings={"data_source": "caller"})
        assert result["status"] == AgentStatus.ERROR.value
        assert "purchase_orders" not in result


class TestCallerRecordsPath:
    def test_caller_records_are_published_as_given(self):
        records = {"purchase_orders": [dict(_PO)], "delivery_records": [], "defect_returns": [], "invoice_records": []}
        result = _execute(_request(records=records))
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["record_source"] == "caller"
        assert result["purchase_orders"] == [_PO]

    def test_caller_records_win_over_the_sample_whatever_the_deployment_says(self):
        records = {"purchase_orders": [dict(_PO)]}
        result = _execute(_request(records=records), domain_settings={"data_source": "caller"})
        assert result["record_source"] == "caller"
        assert result["purchase_orders"] == [_PO]

    def test_filter_follows_the_purchase_order_for_lines_without_a_supplier(self):
        records = {
            "purchase_orders": [dict(_PO), dict(_PO, po_id="PO-2", supplier_id="SUP-2")],
            "delivery_records": [
                {"po_id": "PO-1", "qty_received": 1, "actual_delivery_date": "2026-01-15"},
                {"po_id": "PO-2", "qty_received": 1, "actual_delivery_date": "2026-01-15"},
            ],
        }
        result = _execute(_request(supplier_filter=["SUP-2"], records=records))
        assert [r["po_id"] for r in result["purchase_orders"]] == ["PO-2"]
        assert [r["po_id"] for r in result["delivery_records"]] == ["PO-2"]

    def test_request_reaches_the_node_as_the_inner_user_input(self):
        """The framework hands the inner graph the contract as user_input."""
        result = DataIngestionNode().execute({"user_input": _request()})
        assert result["status"] == AgentStatus.SUCCESS.value


class TestFailClosed:
    @pytest.mark.parametrize(
        "state",
        [
            {},
            {"validated_input": None},
            {"user_input": ""},
            {"user_input": "{}"},
            {"validated_input": {"supplier_filter": []}},
        ],
    )
    def test_missing_request_is_refused(self, state):
        result = DataIngestionNode().execute(state)
        assert result["status"] == AgentStatus.ERROR.value

    def test_an_earlier_error_is_carried_forward(self):
        result = _execute(_request(), status=AgentStatus.ERROR.value)
        assert result == {}
