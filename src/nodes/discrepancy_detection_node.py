"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-158 — DiscrepancyDetectionNode
# Inner domain graph node 2: perform the 3-way match (PO ↔ delivery ↔ invoice)
# and flag discrepancies.
#
# Input:  state["purchase_orders"], state["delivery_records"],
#         state["invoice_records"], state["defect_returns"],
#         state["domain_settings"] (late_delivery_tolerance_days, price_tolerance_pct)
# Output: state["matched_triples"], state["discrepancy_flags"]
#
# Discrepancy types detected:
#   qty_shortfall    — qty_received < qty_ordered
#   late_delivery    — actual_delivery_date > planned_delivery_date + tolerance days
#   invoice_mismatch — |invoiced_price - unit_price| / unit_price > price_tolerance_pct
#   defect_return    — a return record exists for the PO
#
# Every flag carries its measure as a structured field (shortfall, days_late,
# price_deviation_pct, qty_defective) as well as the rendered `detail` line, so
# downstream steps never parse a sentence to recover a number. The detail line
# for an invoice mismatch reports the deviation only — unit prices are
# commercially sensitive and are not rendered into the scorecard.

import logging
from typing import Any, ClassVar, Dict, List, Mapping

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import parse_iso_date, settings_from_state

logger = logging.getLogger(__name__)


def _build_index(records: List[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    """Build a dict index keyed by record[key]. The contract admits one record per PO."""
    return {str(r.get(key, "")): r for r in records}


def _build_multi_index(records: List[Dict[str, Any]], key: str) -> Dict[str, List[Dict[str, Any]]]:
    """Build a dict index mapping key → list of records (returns may repeat per PO)."""
    idx: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        idx.setdefault(str(r.get(key, "")), []).append(r)
    return idx


def _as_int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _as_float(value: Any, default: float) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default


class DiscrepancyDetectionNode(FunctionNode):
    """Perform the 3-way match and flag discrepancies.

    Inner domain graph node 2 (registered as "discrepancy_detection" in DomainWorkflowGraph).

    Input state keys:
        purchase_orders, delivery_records, invoice_records, defect_returns
        domain_settings: the seeded runtime tuning

    Output state keys (partial dict):
        matched_triples:    list of joined records (all matched POs)
        discrepancy_flags:  list of flag dicts
        status:             AgentStatus.SUCCESS or AgentStatus.ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        purchase_orders: List[Dict[str, Any]] = list(state.get("purchase_orders") or [])
        delivery_records: List[Dict[str, Any]] = list(state.get("delivery_records") or [])
        invoice_records: List[Dict[str, Any]] = list(state.get("invoice_records") or [])
        defect_returns: List[Dict[str, Any]] = list(state.get("defect_returns") or [])

        settings = settings_from_state(state)
        late_tolerance_days: int = int(settings["late_delivery_tolerance_days"])
        price_tolerance_pct: float = float(settings["price_tolerance_pct"])

        delivery_idx = _build_index(delivery_records, "po_id")
        invoice_idx = _build_index(invoice_records, "po_id")
        defect_idx = _build_multi_index(defect_returns, "po_id")

        matched_triples: List[Dict[str, Any]] = []
        discrepancy_flags: List[Dict[str, Any]] = []

        for po in purchase_orders:
            if not isinstance(po, Mapping):
                continue
            po_id = str(po.get("po_id", ""))
            supplier_id = str(po.get("supplier_id", ""))
            line_item = str(po.get("line_item", ""))
            qty_ordered = _as_int(po.get("qty_ordered", 0))
            unit_price = _as_float(po.get("unit_price", 0.0), 0.0)
            planned_date_str = str(po.get("planned_delivery_date", ""))

            delivery = delivery_idx.get(po_id, {})
            invoice = invoice_idx.get(po_id, {})

            qty_received = _as_int(delivery.get("qty_received", 0))
            actual_date_str = str(delivery.get("actual_delivery_date", ""))
            invoiced_price = _as_float(invoice.get("invoiced_price", unit_price), unit_price)
            invoiced_qty = _as_int(invoice.get("invoiced_qty", qty_received))

            matched_triples.append(
                {
                    "po_id": po_id,
                    "supplier_id": supplier_id,
                    "line_item": line_item,
                    "qty_ordered": qty_ordered,
                    "qty_received": qty_received,
                    "invoiced_qty": invoiced_qty,
                    "unit_price": unit_price,
                    "invoiced_price": invoiced_price,
                    "planned_delivery_date": planned_date_str,
                    "actual_delivery_date": actual_date_str,
                }
            )

            # --- Flag: quantity shortfall ---
            if qty_received < qty_ordered:
                gap = qty_ordered - qty_received
                severity = "HIGH" if gap / max(qty_ordered, 1) > 0.10 else "MEDIUM"
                discrepancy_flags.append(
                    {
                        "supplier_id": supplier_id,
                        "po_id": po_id,
                        "flag_type": "qty_shortfall",
                        "severity": severity,
                        "shortfall": gap,
                        "detail": f"Ordered {qty_ordered} units; received {qty_received} (shortfall: {gap})",
                    }
                )

            # --- Flag: late delivery ---
            planned_dt = parse_iso_date(planned_date_str)
            actual_dt = parse_iso_date(actual_date_str)
            if planned_dt is not None and actual_dt is not None:
                days_late = (actual_dt - planned_dt).days
                if days_late > late_tolerance_days:
                    severity = "HIGH" if days_late > 7 else "MEDIUM"
                    discrepancy_flags.append(
                        {
                            "supplier_id": supplier_id,
                            "po_id": po_id,
                            "flag_type": "late_delivery",
                            "severity": severity,
                            "days_late": days_late,
                            "detail": f"Planned {planned_date_str}; actual {actual_date_str} ({days_late} day(s) late)",
                        }
                    )

            # --- Flag: invoice price mismatch (deviation only — prices are not rendered) ---
            if unit_price > 0:
                price_diff_pct = abs(invoiced_price - unit_price) / unit_price
                if price_diff_pct > price_tolerance_pct:
                    severity = "HIGH" if price_diff_pct > 0.10 else "MEDIUM"
                    discrepancy_flags.append(
                        {
                            "supplier_id": supplier_id,
                            "po_id": po_id,
                            "flag_type": "invoice_mismatch",
                            "severity": severity,
                            "price_deviation_pct": round(price_diff_pct * 100, 2),
                            "detail": f"Invoiced unit price deviates {price_diff_pct:.1%} from the purchase order",
                        }
                    )

            # --- Flag: defect/return ---
            for defect in defect_idx.get(po_id, []):
                qty_defective = _as_int(defect.get("qty_defective", 0))
                if qty_defective > 0:
                    defect_pct = qty_defective / max(qty_received, 1)
                    severity = "HIGH" if defect_pct > 0.05 else "LOW"
                    reason = str(defect.get("return_reason") or "unspecified")
                    discrepancy_flags.append(
                        {
                            "supplier_id": supplier_id,
                            "po_id": po_id,
                            "flag_type": "defect_return",
                            "severity": severity,
                            "qty_defective": qty_defective,
                            "detail": (
                                f"{qty_defective} units defective/returned ({defect_pct:.1%} of received); reason: {reason}"
                            ),
                        }
                    )

        flag_counts: Dict[str, int] = {}
        for f in discrepancy_flags:
            flag_counts[f["flag_type"]] = flag_counts.get(f["flag_type"], 0) + 1

        logger.info(
            "DiscrepancyDetectionNode: matched %d triples; %d flags raised (%s)",
            len(matched_triples),
            len(discrepancy_flags),
            flag_counts,
        )

        # S-4 audit trail: record the 3-way match side-effect (triples + flags).
        emit_trace_event(
            "discrepancy_detection_complete",
            {
                "po_count": len(purchase_orders),
                "matched_triple_count": len(matched_triples),
                "total_flags": len(discrepancy_flags),
                "flag_counts": flag_counts,
                "late_tolerance_days": late_tolerance_days,
                "price_tolerance_pct": price_tolerance_pct,
            },
            state,
        )

        return {
            "matched_triples": matched_triples,
            "discrepancy_flags": discrepancy_flags,
            "status": AgentStatus.SUCCESS.value,
        }
