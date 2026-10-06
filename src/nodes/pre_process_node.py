"""AgentCore Platform v1.0"""

# Node contract (agents_layer_design.md §1):
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#  - Return AgentStatus enum constants — never plain strings [A1]
#  - Read input_context via state.get("input_context", {}) — read-only [C1]
#  - Never import from mediator/, api/, or other agents
#
# RET-C2-158 — PreProcessNode (outer backbone pre_process slot)
#
# The caller boundary. Everything a caller can send is refused or accepted
# here, once, against the contract in src/services/service.py:
#   * the request object carries the reporting period (YYYY-MM, not in the
#     future, within the look-back window), an optional supplier filter of
#     inert identifiers, and the output format;
#   * the structured parameters carry the purchase-order, delivery, return and
#     invoice records — every field validated against a closed schema, every
#     identifier inert, every number finite and bounded, every date strict;
#   * both channels are screened for chat-template control tokens and
#     instruction-override directives, raw and with markup removed, keys
#     included, and for credential-shaped values;
#   * a refusal names the field and never repeats the value.
#
# The refusal is enforced HERE, in the node that owns the caller contract,
# rather than being left to the platform's own input screen. That screen scores
# some control-token forms and not others, and where it is absent or configured
# off the payload would reach the domain pipeline and return success. The same
# rules are enforced in src/api/server.py at the HTTP door; the two are not
# duplicates, because the platform gateway calls invoke() directly and never
# runs the adapter.
#
# S-1 rules (design §5):
#   - required_trust_level = TrustLevel.VERIFIED_EXTERNAL

import logging
from typing import Any, ClassVar, Dict

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.services.service import ContractError, settings_from_state, validate_request

logger = logging.getLogger(__name__)


class PreProcessNode(FunctionNode):
    """S-1 input validation: validate the supplier report request and its records.

    Outer backbone pre_process slot. Rejects malformed or unauthorized
    invocation payloads before the inner DomainWorkflowGraph runs.

    Input state keys:
        user_input:      JSON string (or mapping) with period, supplier_filter,
                         output_format
        input_context:   the structured records and, optionally, the supplier
                         filter (read-only)
        domain_settings: the seeded runtime tuning (data_source decides
                         whether records are mandatory)

    Output state keys (partial dict):
        validated_input:  the validated contract the inner graph runs on
        status:           AgentStatus.SUCCESS or AgentStatus.ERROR
        formatted_output: (on refusal) a notice naming the field and the rule
        result:           (on refusal) None
        error_log:        (on refusal) the same notice
    """

    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: AgentState) -> Dict[str, Any]:
        user_input = state.get("user_input", "")
        input_context = state.get("input_context", {})  # read-only [C1]
        settings = settings_from_state(state)

        try:
            contract = validate_request(
                user_input,
                input_context,
                require_records=settings["data_source"] == "caller",
            )
        except ContractError as refusal:
            # The field is named so the caller can act; the value never appears.
            #
            # The refusal is also written to formatted_output, because the
            # envelope surfaces that field and nothing else: the error log is
            # not part of it, so a refusal that lived only there would reach
            # the caller as an error with no reason at all. What travels is
            # the field path and a fixed reason phrase — both drawn from the
            # contract's own vocabulary, never a caller value.
            emit_trace_event(
                "request_refused",
                {"field": refusal.field, "reason": refusal.reason},
                state,
            )
            message = f"Request refused — {refusal.field}: {refusal.reason}"
            return {
                "status": AgentStatus.ERROR.value,
                "formatted_output": message,
                "result": None,
                "error_log": [message],
            }

        records = contract["records"] or {}
        supplier_filter = contract["supplier_filter"] or []

        logger.info(
            "PreProcessNode: validated period=%s supplier_filter_count=%d record_source=%s",
            contract["period"],
            len(supplier_filter),
            contract["record_source"],
        )

        # S-4 audit trail: counts only — never a caller value.
        emit_trace_event(
            "pre_process_validated",
            {
                "period": contract["period"],
                "supplier_filter_count": len(supplier_filter),
                "output_format": contract["output_format"],
                "record_source": contract["record_source"],
                "record_counts": {name: len(items) for name, items in records.items()},
            },
            state,
        )

        return {
            "validated_input": contract,
            "status": AgentStatus.SUCCESS.value,
        }
