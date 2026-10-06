"""AgentCore Platform v1.0"""

# ADR-005: State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
#
# RET-C2-158 — Supplier Delivery Performance & Order Discrepancy Report
# Two-layer nested Cat 2 graph: outer backbone (AgentBaseGraph) + inner
# domain workflow (BaseGraph).  Fields below cover both layers.
#
# Every caller-derived value held here has passed the closed contract in
# src/services/service.py: identifiers are inert tokens, quantities and
# prices are finite bounded numbers, dates are strict calendar dates. Nothing
# written by a node carries free text from the caller.

from typing import Any, Dict, List, Optional

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Flat TypedDict for RET-C2-158.

    All shared fields (user_input, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState.
    """

    # ------------------------------------------------------------------
    # Outer layer — set by PreProcessNode / SupplierReportGraphNode.merge_output
    # ------------------------------------------------------------------

    # The validated request contract produced by PreProcessNode:
    # {"period": "YYYY-MM", "supplier_filter": [...] | None,
    #  "output_format": "markdown", "records": {...} | None,
    #  "record_source": "caller" | "baseline"}
    # It is also the request object the inner graph is invoked with.
    validated_input: Optional[Dict[str, Any]]

    # Final validated supplier performance report.
    # Written by merge_output() from the inner graph's report_markdown output
    # and read by PostProcessNode, the output gate.
    result: Optional[str]

    # ------------------------------------------------------------------
    # Runtime tuning — seeded from config/config.yaml by both graphs
    # ------------------------------------------------------------------

    # The validated ret_c2_158 block: data_source, late_delivery_tolerance_days,
    # price_tolerance_pct, kpi_weights. Node execute() methods take no config
    # argument, so this is how a declared value reaches a domain node.
    domain_settings: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Inner layer — domain nodes (DomainWorkflowGraph)
    # ------------------------------------------------------------------

    # DataIngestionNode output
    # Purchase order records for the target period.
    # Each record: {"po_id": str, "supplier_id": str, "line_item": str,
    #               "qty_ordered": int, "unit_price": float,
    #               "planned_delivery_date": str, "status": str}
    purchase_orders: Optional[List[Dict[str, Any]]]

    # DataIngestionNode output
    # Goods receipt / delivery note records.
    # Each record: {"po_id": str, "qty_received": int, "actual_delivery_date": str}
    delivery_records: Optional[List[Dict[str, Any]]]

    # DataIngestionNode output
    # Defect and return records.
    # Each record: {"po_id": str, "qty_defective": int, "return_reason": str}
    defect_returns: Optional[List[Dict[str, Any]]]

    # DataIngestionNode output
    # Invoice records.
    # Each record: {"po_id": str, "invoiced_price": float, "invoiced_qty": int}
    invoice_records: Optional[List[Dict[str, Any]]]

    # DataIngestionNode output
    # "caller" when the records came from the request, "baseline" when the
    # built-in sample dataset was used. Rendered into the scorecard so a reader
    # can never mistake the sample for real data.
    record_source: Optional[str]

    # DiscrepancyDetectionNode output
    # 3-way matched records (PO + delivery + invoice joined).
    matched_triples: Optional[List[Dict[str, Any]]]

    # DiscrepancyDetectionNode output
    # Discrepancy flags raised during 3-way matching.
    # Each entry: {"supplier_id": str, "po_id": str, "flag_type": str,
    #              "severity": str, "detail": str, ...structured measures}
    # flag_type: "qty_shortfall" | "late_delivery" | "invoice_mismatch" | "defect_return"
    # severity: "HIGH" | "MEDIUM" | "LOW"
    discrepancy_flags: Optional[List[Dict[str, Any]]]

    # PerformanceMetricsNode output
    # Per-supplier KPI dictionary.
    # {supplier_id: {"otd_pct": float, "fill_rate": float,
    #                "otif_pct": float, "defect_pct": float,
    #                "invoice_accuracy_pct": float, "composite_score": float}}
    supplier_kpis: Optional[Dict[str, Any]]

    # PerformanceMetricsNode output
    # Supplier ranking list sorted by composite_score descending.
    # Each entry: {"rank": int, "supplier_id": str, "composite_score": float}
    supplier_rankings: Optional[List[Dict[str, Any]]]

    # ReportGenerationNode output
    # The supplier performance scorecard in markdown, rendered
    # deterministically from the KPI data. It must pass the output gate before
    # surfacing to the caller.
    report_markdown: Optional[str]

    # ReportGenerationNode output
    # Report metadata.
    # Keys: period (str), generated_at (str), supplier_count (int),
    #       alert_count (int), record_source (str)
    report_metadata: Optional[Dict[str, Any]]

    # ------------------------------------------------------------------
    # Tracing / audit — framework-managed; do NOT write from node code
    # ------------------------------------------------------------------

    trace_id: Optional[str]
    correlation_id: Optional[str]
    # node_history inherited from AgentState; listed here for clarity
    # node_history: Optional[List[str]]
