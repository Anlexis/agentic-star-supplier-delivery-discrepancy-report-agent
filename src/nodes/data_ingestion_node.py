"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-158 — DataIngestionNode
# Inner domain graph node 1: publish the purchase-order, delivery, return and
# invoice records the rest of the pipeline matches.
#
# Input:  the validated request contract (period, supplier_filter, records)
#         and the seeded runtime settings (data_source)
# Output: state["purchase_orders"], state["delivery_records"],
#         state["defect_returns"], state["invoice_records"],
#         state["record_source"]
#
# Records supplied by the caller are always used. When the request carries
# none, `data_source: mock` publishes the built-in sample dataset and
# `data_source: caller` refuses — a deployment on real data must never answer
# a request that carried no data with a scorecard computed from the sample.

import logging
from typing import Any, ClassVar, Dict, List, Mapping, Optional

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import RECORD_LISTS, settings_from_state

logger = logging.getLogger(__name__)

RECORD_SOURCE_CALLER = "caller"
RECORD_SOURCE_BASELINE = "baseline"


def _apply_supplier_filter(
    data: Dict[str, List[Dict[str, Any]]], supplier_filter: Optional[List[str]]
) -> Dict[str, List[Dict[str, Any]]]:
    """Keep the purchase orders of the filtered suppliers and the lines that reference them.

    Deliveries, returns and invoices are filtered through the purchase order
    they reference rather than through a supplier field of their own, so a line
    that carries no supplier id is never silently dropped from a filtered
    request.
    """
    if not supplier_filter:
        return data
    keep = set(supplier_filter)
    orders = [order for order in data["purchase_orders"] if str(order.get("supplier_id", "")) in keep]
    kept_ids = {str(order.get("po_id", "")) for order in orders}
    return {
        "purchase_orders": orders,
        "delivery_records": [r for r in data["delivery_records"] if str(r.get("po_id", "")) in kept_ids],
        "defect_returns": [r for r in data["defect_returns"] if str(r.get("po_id", "")) in kept_ids],
        "invoice_records": [r for r in data["invoice_records"] if str(r.get("po_id", "")) in kept_ids],
    }


def _load_mock_data(period: str) -> Dict[str, List[Dict[str, Any]]]:
    """The built-in sample dataset for *period*: three purchase orders, two suppliers.

    SUP-001 has one quantity shortfall with a return and one late delivery with
    an invoice price mismatch; SUP-002 has one clean order. Every value satisfies
    the same closed contract caller records must, so the sample and a real
    request render through one path.
    """
    return {
        "purchase_orders": [
            {
                "po_id": f"PO-{period}-001",
                "supplier_id": "SUP-001",
                "line_item": "WIDGET-A",
                "qty_ordered": 100,
                "unit_price": 5.00,
                "planned_delivery_date": f"{period}-15",
                "status": "delivered",
            },
            {
                "po_id": f"PO-{period}-002",
                "supplier_id": "SUP-001",
                "line_item": "WIDGET-B",
                "qty_ordered": 50,
                "unit_price": 12.00,
                "planned_delivery_date": f"{period}-20",
                "status": "delivered",
            },
            {
                "po_id": f"PO-{period}-003",
                "supplier_id": "SUP-002",
                "line_item": "GADGET-X",
                "qty_ordered": 200,
                "unit_price": 8.50,
                "planned_delivery_date": f"{period}-10",
                "status": "delivered",
            },
        ],
        "delivery_records": [
            {"po_id": f"PO-{period}-001", "qty_received": 95, "actual_delivery_date": f"{period}-15"},
            {"po_id": f"PO-{period}-002", "qty_received": 50, "actual_delivery_date": f"{period}-25"},
            {"po_id": f"PO-{period}-003", "qty_received": 200, "actual_delivery_date": f"{period}-09"},
        ],
        "defect_returns": [
            {"po_id": f"PO-{period}-001", "qty_defective": 3, "return_reason": "cosmetic_defect"},
        ],
        "invoice_records": [
            {"po_id": f"PO-{period}-001", "invoiced_price": 5.00, "invoiced_qty": 95},
            {"po_id": f"PO-{period}-002", "invoiced_price": 12.50, "invoiced_qty": 50},
            {"po_id": f"PO-{period}-003", "invoiced_price": 8.50, "invoiced_qty": 200},
        ],
    }


def _request_from_state(state: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The validated request: seeded as validated_input, or as the inner graph's user_input.

    The framework invokes the inner graph with the outer main slot's
    extract_input() value as `user_input`, which is the validated contract dict.
    Nothing here parses raw caller text — an unvalidated request cannot reach
    this node through either key, because the outer pre_process slot either
    publishes the contract or refuses the run.
    """
    for key in ("validated_input", "user_input"):
        candidate = state.get(key)
        if isinstance(candidate, Mapping) and candidate.get("period"):
            return dict(candidate)
    return None


class DataIngestionNode(FunctionNode):
    """Publish the purchase-order, delivery, return and invoice records for the period.

    Inner domain graph node 1 (registered as "data_ingestion" in DomainWorkflowGraph).

    Input state keys:
        validated_input / user_input: the validated request contract
        domain_settings:              the seeded runtime tuning

    Output state keys (partial dict):
        purchase_orders, delivery_records, defect_returns, invoice_records
        record_source: "caller" or "baseline"
        status:        AgentStatus.SUCCESS or AgentStatus.ERROR
        error_log:     (on error) list of error messages
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        # An earlier step's failure is carried forward, never overwritten with
        # success: the topology is linear, so this node runs regardless.
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        request = _request_from_state(state)
        if request is None:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["DataIngestionNode: the validated request is missing — the request boundary did not run"],
            }

        settings = settings_from_state(state)
        period: str = str(request["period"])
        supplier_filter = request.get("supplier_filter")
        records = request.get("records")

        if isinstance(records, Mapping):
            data = {name: list(records.get(name) or []) for name in RECORD_LISTS}
            record_source = RECORD_SOURCE_CALLER
        elif settings["data_source"] == "mock":
            data = _load_mock_data(period)
            record_source = RECORD_SOURCE_BASELINE
        else:
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["DataIngestionNode: no records were supplied and data_source is caller"],
            }

        data = _apply_supplier_filter(data, supplier_filter if isinstance(supplier_filter, list) else None)

        logger.info(
            "DataIngestionNode: ingested period=%s source=%s po=%d delivery=%d defect=%d invoice=%d",
            period,
            record_source,
            len(data["purchase_orders"]),
            len(data["delivery_records"]),
            len(data["defect_returns"]),
            len(data["invoice_records"]),
        )

        # S-4 audit trail: record the ingest side-effect (records published per type).
        emit_trace_event(
            "data_ingestion_complete",
            {
                "period": period,
                "record_source": record_source,
                "po_count": len(data["purchase_orders"]),
                "delivery_count": len(data["delivery_records"]),
                "defect_count": len(data["defect_returns"]),
                "invoice_count": len(data["invoice_records"]),
                "supplier_filter_active": bool(supplier_filter),
            },
            state,
        )

        return {
            "purchase_orders": data["purchase_orders"],
            "delivery_records": data["delivery_records"],
            "defect_returns": data["defect_returns"],
            "invoice_records": data["invoice_records"],
            "record_source": record_source,
            "status": AgentStatus.SUCCESS.value,
        }
