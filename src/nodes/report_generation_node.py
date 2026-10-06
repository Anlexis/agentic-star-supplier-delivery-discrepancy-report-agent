"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-158 — ReportGenerationNode
# Inner domain graph node 4: render the monthly supplier performance scorecard
# in markdown, deterministically, from the computed KPI data.
#
# Input:  state["supplier_kpis"], state["supplier_rankings"],
#         state["discrepancy_flags"], state["validated_input"] / state["user_input"],
#         state["record_source"]
# Output: state["report_markdown"], state["result"], state["report_metadata"]
#
# No model is invoked. Every rendered value is either a validated inert
# identifier, a validated date, or a number computed from validated inputs, so
# no caller free text can reach the scorecard. Whether the scorecard was built
# from the caller's records or from the built-in sample dataset is stated in
# the report itself.

import logging
from datetime import datetime, timezone
from typing import Any, ClassVar, Dict, List, Mapping

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

logger = logging.getLogger(__name__)

# Alert rows rendered in the scorecard; the full flag list stays in state.
MAX_ALERT_ROWS = 10

_REPORT_TEMPLATE = """\
# Supplier Performance Scorecard — {period}

> Generated: {generated_at} | Suppliers evaluated: {supplier_count} | Alerts: {alert_count}
> Data: {data_line}

## Executive Summary

{summary}

## KPI Summary Table

| Supplier | OTD% | Fill Rate% | OTIF% | Defect% | Invoice Acc% | Score |
|----------|------|-----------|-------|---------|-------------|-------|
{kpi_table_rows}

## Supplier Rankings

{ranking_rows}

## Alert Flags

{alert_rows}

## Action Recommendations

{recommendations}
"""


def _data_line(record_source: str, po_count: int) -> str:
    if record_source == "caller":
        return f"{po_count} purchase order(s) supplied with the request"
    return "the built-in sample dataset (no records were supplied with the request)"


def _summary(supplier_kpis: Mapping[str, Any], flags: List[Dict[str, Any]], po_count: int) -> str:
    high = sum(1 for f in flags if f.get("severity") == "HIGH")
    counts: Dict[str, int] = {}
    for f in flags:
        counts[str(f.get("flag_type", ""))] = counts.get(str(f.get("flag_type", "")), 0) + 1
    if not supplier_kpis:
        return "No purchase orders matched the request, so no supplier KPIs were computed."
    breakdown = ", ".join(f"{name}: {count}" for name, count in sorted(counts.items())) or "none"
    return (
        f"{po_count} purchase order(s) across {len(supplier_kpis)} supplier(s) were matched against "
        f"deliveries, returns and invoices. {len(flags)} discrepancy flag(s) were raised "
        f"({high} high severity). Flags by type — {breakdown}."
    )


def _build_kpi_table_rows(supplier_kpis: Mapping[str, Any]) -> str:
    if not supplier_kpis:
        return "| (no suppliers) | — | — | — | — | — | — |"
    rows = []
    for sid in sorted(supplier_kpis):
        kpi = supplier_kpis[sid]
        rows.append(
            f"| {sid} | {kpi.get('otd_pct', 0):.1f} | {kpi.get('fill_rate', 0):.1f} "
            f"| {kpi.get('otif_pct', 0):.1f} | {kpi.get('defect_pct', 0):.1f} "
            f"| {kpi.get('invoice_accuracy_pct', 0):.1f} | {kpi.get('composite_score', 0):.1f} |"
        )
    return "\n".join(rows)


def _build_ranking_rows(supplier_rankings: List[Dict[str, Any]]) -> str:
    if not supplier_rankings:
        return "No suppliers to rank."
    return "\n".join(
        f"{r['rank']}. **{r['supplier_id']}** — composite score: {r['composite_score']:.1f}" for r in supplier_rankings
    )


def _build_alert_rows(discrepancy_flags: List[Dict[str, Any]]) -> str:
    high_flags = [f for f in discrepancy_flags if f.get("severity") == "HIGH"]
    if not high_flags:
        return "No HIGH-severity alerts this period."
    rows = [
        f"- **[{str(f['flag_type']).upper()}]** {f['supplier_id']} / {f['po_id']}: {f['detail']}"
        for f in high_flags[:MAX_ALERT_ROWS]
    ]
    if len(high_flags) > MAX_ALERT_ROWS:
        rows.append(f"- ... and {len(high_flags) - MAX_ALERT_ROWS} further HIGH-severity flag(s)")
    return "\n".join(rows)


def _build_recommendations(supplier_kpis: Mapping[str, Any], discrepancy_flags: List[Dict[str, Any]]) -> str:
    """Action items derived from the KPI data — one line per condition actually met."""
    items: List[str] = []
    for sid in sorted(supplier_kpis):
        kpi = supplier_kpis[sid]
        if kpi.get("otd_pct", 100.0) < 90.0:
            items.append(f"- Escalate the late-delivery pattern for {sid} (OTD {kpi['otd_pct']:.1f}% is below 90%).")
        if kpi.get("defect_pct", 0.0) > 5.0:
            items.append(f"- Request a quality improvement plan from {sid} (defect rate {kpi['defect_pct']:.1f}%).")
        if kpi.get("invoice_accuracy_pct", 100.0) < 100.0:
            items.append(
                f"- Reconcile invoice pricing with {sid} (invoice accuracy {kpi['invoice_accuracy_pct']:.1f}%)."
            )
    shortfalls = [f for f in discrepancy_flags if f.get("flag_type") == "qty_shortfall"]
    if shortfalls:
        items.append(f"- Follow up {len(shortfalls)} quantity shortfall(s) for replacement or credit.")
    return "\n".join(items) if items else "- No action items this period."


class ReportGenerationNode(FunctionNode):
    """Render the supplier performance scorecard.

    Inner domain graph node 4 (registered as "report_generation" in DomainWorkflowGraph).

    Input state keys:
        supplier_kpis, supplier_rankings, discrepancy_flags, matched_triples
        validated_input / user_input: the validated request (period)
        record_source: "caller" or "baseline"

    Output state keys (partial dict):
        report_markdown:  full scorecard in markdown
        result:           same as report_markdown (the output gate reads result)
        report_metadata:  {period, generated_at, supplier_count, alert_count, record_source}
        status:           AgentStatus.SUCCESS or AgentStatus.ERROR
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}

        supplier_kpis: Dict[str, Any] = dict(state.get("supplier_kpis") or {})
        supplier_rankings: List[Dict[str, Any]] = list(state.get("supplier_rankings") or [])
        discrepancy_flags: List[Dict[str, Any]] = list(state.get("discrepancy_flags") or [])
        matched_triples: List[Dict[str, Any]] = list(state.get("matched_triples") or [])

        request: Any = state.get("validated_input")
        if not isinstance(request, Mapping):
            request = state.get("user_input")
        period = str(request.get("period", "unknown")) if isinstance(request, Mapping) else "unknown"
        record_source = str(state.get("record_source") or "baseline")

        generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        supplier_count = len(supplier_kpis)
        alert_count = len([f for f in discrepancy_flags if f.get("severity") == "HIGH"])

        report_metadata: Dict[str, Any] = {
            "period": period,
            "generated_at": generated_at,
            "supplier_count": supplier_count,
            "alert_count": alert_count,
            "record_source": record_source,
        }

        report_markdown = _REPORT_TEMPLATE.format(
            period=period,
            generated_at=generated_at,
            supplier_count=supplier_count,
            alert_count=alert_count,
            data_line=_data_line(record_source, len(matched_triples)),
            summary=_summary(supplier_kpis, discrepancy_flags, len(matched_triples)),
            kpi_table_rows=_build_kpi_table_rows(supplier_kpis),
            ranking_rows=_build_ranking_rows(supplier_rankings),
            alert_rows=_build_alert_rows(discrepancy_flags),
            recommendations=_build_recommendations(supplier_kpis, discrepancy_flags),
        )

        logger.info(
            "ReportGenerationNode: scorecard rendered — period=%s %d chars %d alerts",
            period,
            len(report_markdown),
            alert_count,
        )

        # S-4 audit trail: record the report-rendering side-effect.
        emit_trace_event(
            "report_generation_complete",
            {
                "period": period,
                "supplier_count": supplier_count,
                "alert_count": alert_count,
                "report_length": len(report_markdown),
                "record_source": record_source,
            },
            state,
        )

        return {
            "report_markdown": report_markdown,
            "result": report_markdown,
            "report_metadata": report_metadata,
            "status": AgentStatus.SUCCESS.value,
        }
