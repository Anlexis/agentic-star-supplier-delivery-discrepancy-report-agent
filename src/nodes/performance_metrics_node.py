"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-158 — PerformanceMetricsNode
# Inner domain graph node 3: calculate per-supplier KPIs from matched triples
# and discrepancy flags.
#
# KPIs computed per supplier:
#   OTD%            On-Time Delivery = on-time deliveries / total deliveries × 100
#   Fill Rate%      qty_received / qty_ordered × 100 (aggregate)
#   OTIF%           On-Time In-Full = deliveries that are BOTH on-time AND full quantity
#   Defect%         defect_qty / total_received_qty × 100
#   Invoice Acc%    invoices matching PO unit price / total invoices × 100
#   Composite Score weighted average of the 5 KPIs (weights from the runtime tuning)
#
# Input:  state["matched_triples"], state["discrepancy_flags"],
#         state["domain_settings"] (kpi_weights)
# Output: state["supplier_kpis"], state["supplier_rankings"]

import logging
from typing import Any, ClassVar, Dict, List, Mapping

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import settings_from_state

logger = logging.getLogger(__name__)


def _safe_pct(numerator: float, denominator: float) -> float:
    """Return numerator / denominator * 100, guarded against division by zero."""
    if denominator == 0:
        return 0.0
    return round(numerator / denominator * 100, 2)


def _compute_composite(kpi: Mapping[str, float], weights: Mapping[str, float]) -> float:
    """Compute a 0–100 composite score from KPIs.

    defect_pct is inverted (100 - defect_pct) so lower defects yield a higher score.
    """
    score = (
        weights["otd_pct"] * kpi.get("otd_pct", 0.0)
        + weights["fill_rate"] * kpi.get("fill_rate", 0.0)
        + weights["otif_pct"] * kpi.get("otif_pct", 0.0)
        + weights["defect_pct"] * (100.0 - kpi.get("defect_pct", 0.0))
        + weights["invoice_accuracy_pct"] * kpi.get("invoice_accuracy_pct", 0.0)
    )
    return round(score, 2)


def _as_int(value: Any) -> int:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


class PerformanceMetricsNode(FunctionNode):
    """Calculate per-supplier KPIs and produce a ranked supplier list.

    Inner domain graph node 3 (registered as "performance_metrics" in DomainWorkflowGraph).

    Input state keys:
        matched_triples:   list of 3-way matched records (from DiscrepancyDetectionNode)
        discrepancy_flags: list of flag dicts
        domain_settings:   the seeded runtime tuning (kpi_weights)

    Output state keys (partial dict):
        supplier_kpis:     dict {supplier_id: {otd_pct, fill_rate, otif_pct,
                                               defect_pct, invoice_accuracy_pct,
                                               composite_score}}
        supplier_rankings: list of {rank, supplier_id, composite_score} sorted desc
        status:            AgentStatus.SUCCESS or AgentStatus.ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        matched_triples: List[Dict[str, Any]] = list(state.get("matched_triples") or [])
        discrepancy_flags: List[Dict[str, Any]] = list(state.get("discrepancy_flags") or [])
        weights: Dict[str, float] = dict(settings_from_state(state)["kpi_weights"])

        # Defect quantities from the structured flag field — never parsed out of
        # the rendered detail line.
        defect_by_supplier: Dict[str, int] = {}
        late_by_po: set[str] = set()
        mismatch_by_po: set[str] = set()
        for flag in discrepancy_flags:
            flag_type = flag.get("flag_type")
            if flag_type == "defect_return":
                sid = str(flag.get("supplier_id", ""))
                defect_by_supplier[sid] = defect_by_supplier.get(sid, 0) + _as_int(flag.get("qty_defective", 0))
            elif flag_type == "late_delivery":
                late_by_po.add(str(flag.get("po_id", "")))
            elif flag_type == "invoice_mismatch":
                mismatch_by_po.add(str(flag.get("po_id", "")))

        accum: Dict[str, Dict[str, int]] = {}
        for triple in matched_triples:
            sid = str(triple.get("supplier_id", ""))
            po_id = str(triple.get("po_id", ""))
            qty_ordered = _as_int(triple.get("qty_ordered", 0))
            qty_received = _as_int(triple.get("qty_received", 0))

            a = accum.setdefault(
                sid,
                {
                    "total_ordered": 0,
                    "total_received": 0,
                    "total_deliveries": 0,
                    "on_time_count": 0,
                    "full_qty_count": 0,
                    "otif_count": 0,
                    "total_invoices": 0,
                    "matching_invoice_count": 0,
                },
            )
            a["total_ordered"] += qty_ordered
            a["total_received"] += qty_received
            a["total_deliveries"] += 1

            is_on_time = po_id not in late_by_po
            is_full_qty = qty_received >= qty_ordered
            if is_on_time:
                a["on_time_count"] += 1
            if is_full_qty:
                a["full_qty_count"] += 1
            if is_on_time and is_full_qty:
                a["otif_count"] += 1

            a["total_invoices"] += 1
            if po_id not in mismatch_by_po:
                a["matching_invoice_count"] += 1

        supplier_kpis: Dict[str, Any] = {}
        for sid, a in accum.items():
            kpi: Dict[str, float] = {
                "otd_pct": _safe_pct(a["on_time_count"], a["total_deliveries"]),
                "fill_rate": _safe_pct(a["total_received"], a["total_ordered"]),
                "otif_pct": _safe_pct(a["otif_count"], a["total_deliveries"]),
                "defect_pct": _safe_pct(defect_by_supplier.get(sid, 0), a["total_received"]),
                "invoice_accuracy_pct": _safe_pct(a["matching_invoice_count"], a["total_invoices"]),
                "composite_score": 0.0,
            }
            kpi["composite_score"] = _compute_composite(kpi, weights)
            supplier_kpis[sid] = kpi

        supplier_rankings: List[Dict[str, Any]] = [
            {"rank": rank, "supplier_id": sid, "composite_score": kpi["composite_score"]}
            for rank, (sid, kpi) in enumerate(
                sorted(supplier_kpis.items(), key=lambda x: (-x[1]["composite_score"], x[0])),
                start=1,
            )
        ]

        top_performer = supplier_rankings[0]["supplier_id"] if supplier_rankings else ""
        bottom_performer = supplier_rankings[-1]["supplier_id"] if supplier_rankings else ""

        logger.info(
            "PerformanceMetricsNode: computed KPIs for %d suppliers; top=%s bottom=%s",
            len(supplier_kpis),
            top_performer,
            bottom_performer,
        )

        # S-4 audit trail: record the KPI computation side-effect (per-supplier scores).
        emit_trace_event(
            "performance_metrics_calculated",
            {
                "supplier_count": len(supplier_kpis),
                "top_performer": top_performer,
                "bottom_performer": bottom_performer,
            },
            state,
        )

        return {
            "supplier_kpis": supplier_kpis,
            "supplier_rankings": supplier_rankings,
            "status": AgentStatus.SUCCESS.value,
        }
