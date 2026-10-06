"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-158 — PostProcessNode: the output boundary of the agent.
#
# The stated output invariant of this template: nothing credential-shaped
# leaves the agent, and the scorecard renders only inert identifiers, validated
# dates and numbers computed from validated inputs. This node enforces the
# first half on the whole released surface — the rendered scorecard AND the
# structured metadata behind it — with the recursive scan in
# src/services/service.py. The second half is enforced at the request
# boundary, where the alphabet is closed before anything is rendered.
#
# The credential half takes the UNION of the platform's own detector and the
# local patterns. That is not a style choice. The platform scans every value of
# every node result for credentials and RAISES when it finds one; the wrapper
# then discards this node's whole return value, the clearing included, and the
# envelope falls back to the un-gated report still sitting in state. So a local
# list narrower than the platform's is not a weaker gate — it is a containment
# bypass. And a set narrower than the local one is the converse bypass: the
# platform's patterns describe credential formats and match no part of an
# assignment line such as `password=...`.
#
# Containment on violation: returning an error is not enough on its own. The
# framework's envelope resolves the output as `formatted_output or result` with
# no status check, so a gate that raised — or that set an error status without
# clearing — still ships the un-gated report inside the error envelope. This
# node therefore CLEARS every output-bearing field as it blocks, and replaces
# formatted_output with a TRUTHY notice: a falsy replacement re-opens the very
# fallback the clearing exists to close.

import logging
from typing import Any, ClassVar, Dict, Tuple

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import MAX_REPORT_CHARS, scan_released_content

logger = logging.getLogger(__name__)

# The replacement is TRUTHY on purpose. The framework's envelope falls back to
# state["result"] whenever formatted_output is falsy, so an empty string here
# would re-open the exact channel the clearing closes.
BLOCKED_NOTICE = (
    "[OUTPUT WITHHELD — the supplier performance scorecard did not pass the output "
    "boundary. Resend the records without credential-like strings.]"
)
NO_REPORT_NOTICE = "[NO REPORT — the request produced no scorecard.]"
OVERSIZED_NOTICE = "[OUTPUT WITHHELD — the rendered scorecard exceeds the size the agent releases.]"

# Every state field AT THIS LEVEL that can carry released text. On a violation
# each one is overwritten, so no path out of the graph — including the
# framework's own fallback to state["result"] — can reach the un-gated report.
OUTPUT_BEARING_FIELDS: Tuple[str, ...] = ("result", "formatted_output", "report_markdown", "llm_response")


def _withhold(state: AgentState, notice: str, reason: str) -> Dict[str, Any]:
    """Refuse to release: report the error, clear every output-bearing field, publish a truthy notice."""
    logger.error("PostProcessNode: output withheld — %s", reason)
    emit_trace_event("output_withheld", {"violation": reason}, state)
    blocked: Dict[str, Any] = {field: None for field in OUTPUT_BEARING_FIELDS}
    blocked.update(
        {
            "formatted_output": notice,
            "result": notice,
            "status": AgentStatus.ERROR.value,
            "error_log": [f"Output withheld at the boundary — {reason}"],
        }
    )
    return blocked


class PostProcessNode(FunctionNode):
    """Output gate: refuse to release credential-like content, and contain it when refused.

    Outer backbone post_process slot. Reads state["result"] (the merged
    report from SupplierReportGraphNode.merge_output()) and the structured
    report_metadata released alongside it.

    Output state keys (partial dict):
        formatted_output: the scorecard when clean; a truthy withheld notice on
                          a violation — never an empty value, which would
                          re-open the envelope's fallback
        result:           gated alongside formatted_output
        report_markdown:  cleared on a violation
        status:           AgentStatus.SUCCESS or AgentStatus.ERROR
        error_log:        (on a violation) one closed-set reason label
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        result = state.get("result")
        report_metadata = state.get("report_metadata") or {}

        if not isinstance(result, str) or not result.strip():
            # Nothing was rendered. The notice is still truthy: an empty
            # formatted_output would let the envelope fall back to whatever
            # remains in result.
            return _withhold(state, NO_REPORT_NOTICE, "no_report_rendered")

        if len(result) > MAX_REPORT_CHARS:
            return _withhold(state, OVERSIZED_NOTICE, "output_too_large")

        # The metadata is released alongside the rendered scorecard, so it is
        # gated with it, nested values included.
        violation = scan_released_content({"result": result, "report_metadata": report_metadata})
        if violation:
            return _withhold(state, BLOCKED_NOTICE, violation)

        logger.info(
            "PostProcessNode: output gate PASS — %d chars, period=%s",
            len(result),
            report_metadata.get("period", "") if isinstance(report_metadata, dict) else "",
        )

        # S-4 audit trail: record the gate PASS side-effect (output cleared for return).
        emit_trace_event(
            "post_process_complete",
            {
                "result_length": len(result),
                "supplier_count": report_metadata.get("supplier_count", 0) if isinstance(report_metadata, dict) else 0,
                "alert_count": report_metadata.get("alert_count", 0) if isinstance(report_metadata, dict) else 0,
                "period": report_metadata.get("period", "") if isinstance(report_metadata, dict) else "",
            },
            state,
        )

        return {
            "formatted_output": result,
            "result": result,
            "status": AgentStatus.SUCCESS.value,
        }
