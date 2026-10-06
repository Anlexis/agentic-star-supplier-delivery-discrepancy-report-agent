# The output boundary — what the agent refuses to release, and what it clears
# when it refuses.
#
# The stated invariant: nothing credential-shaped leaves the agent, and the
# scorecard renders only inert identifiers, validated dates and computed
# numbers. Enforced on the whole released surface, nested structures included.
#
# Two properties are pinned here that a gate can appear to have without having:
#
#   * DETECTOR PARITY. The platform scans every value of every node result for
#     credentials and RAISES when it finds one; the wrapper then discards this
#     node's whole return value, the clearing included, and the envelope falls
#     back to the un-gated report still in state. So a local pattern list
#     narrower than the platform's is not a weaker gate — it is a bypass. Every
#     shape the platform recognises is probed here, against the platform's own
#     detector so a shape the platform stops recognising makes the probe fail
#     rather than pass vacuously.
#
#   * CLEARING, ASSERTED AS PRESENCE AND EMPTINESS. Partial state updates are
#     MERGED, so omitting a key leaves the old value in state — and
#     `assert not result.get(field)` then passes on a gate that cleared
#     nothing. Each assertion below requires the key to be in the returned
#     update AND to carry the cleared value.
#
# Deterministic — no model, no network.

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.security.credential_detector import detect_credentials

from src.nodes.post_process_node import BLOCKED_NOTICE, OUTPUT_BEARING_FIELDS, PostProcessNode
from src.services.service import scan_released_content

# Simulated shapes — not real credentials.
_SHAPES = {
    "openai_key": "sk-TESTKEY1234567890abcdefghij",
    "stripe_key": "sk_live_" + "TESTKEY1234567890abcd",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c",
    "aws_key": "AKIAIOSFODNN7EXAMPLE",
    "bearer_token": "Bearer abcdefghijklmnopqrstuvwxyz",
    "conn_string": "postgresql://db.internal.example:5432/procurement",
}

_CLEAN_REPORT = (
    "# Supplier Performance Scorecard — 2026-01\n\n"
    "| SUP-ALPHA | 50.0 | 96.7 | 0.0 | 2.1 | 50.0 | 58.9 |\n"
    "- **[LATE_DELIVERY]** SUP-ALPHA / PO-2026-01-0002: Planned 2026-01-20; actual 2026-02-05 (16 day(s) late)\n"
)


def _report_with(value: str) -> str:
    return f"{_CLEAN_REPORT}\nnote: {value}\n"


def _block(value: str) -> dict:
    return PostProcessNode().execute({"result": _report_with(value), "report_markdown": _report_with(value)})


class TestDetectorParity:
    """Every shape the platform recognises must be refused HERE first."""

    @pytest.mark.parametrize("name,value", sorted(_SHAPES.items()))
    def test_platform_recognises_the_probe(self, name, value):
        """Guard on the probes themselves: a shape the platform ignores would make parity vacuous."""
        assert detect_credentials(value), f"probe for {name} no longer matches"

    @pytest.mark.parametrize("name,value", sorted(_SHAPES.items()))
    def test_gate_refuses_every_platform_shape(self, name, value):
        assert scan_released_content(_report_with(value)) is not None

    @pytest.mark.parametrize("name,value", sorted(_SHAPES.items()))
    def test_block_is_attributed_to_this_gate(self, name, value):
        """The block must come from the node, not from the platform raising."""
        result = _block(value)
        assert result["status"] == AgentStatus.ERROR.value
        assert result["error_log"]

    @pytest.mark.parametrize(
        "value",
        [
            "password = hunter2hunter2",
            "-----BEGIN RSA PRIVATE KEY-----",
            "pk-TESTKEY1234567890abcdef",
            "Bearer abc123def456",
        ],
    )
    def test_gate_also_covers_shapes_the_platform_does_not(self, value):
        assert not detect_credentials(value), "the probe is meant to be a shape the platform misses"
        assert scan_released_content(value) is not None

    def test_reason_is_a_label_never_the_matched_text(self):
        result = _block(_SHAPES["aws_key"])
        assert _SHAPES["aws_key"] not in str(result)
        assert "aws_key" in result["error_log"][0]


class TestNestedSurface:
    def test_nested_leak_is_found(self):
        assert scan_released_content({"report_metadata": {"notes": {"upstream": _SHAPES["aws_key"]}}}) is not None

    def test_leak_inside_a_list_is_found(self):
        assert scan_released_content([{"note": _SHAPES["jwt"]}]) is not None

    def test_clean_nested_structure_passes(self):
        """The control that proves the scan is not simply refusing everything."""
        assert scan_released_content({"report_metadata": {"period": "2026-01", "supplier_count": 2}}) is None


class TestClearingOnViolation:
    """Presence AND emptiness — merged updates make omission look like clearing."""

    @pytest.mark.parametrize("field", OUTPUT_BEARING_FIELDS)
    def test_every_output_bearing_field_is_present_in_the_update(self, field):
        assert field in _block(_SHAPES["aws_key"])

    def test_report_field_is_emptied(self):
        assert _block(_SHAPES["aws_key"])["report_markdown"] is None

    def test_released_text_is_replaced_not_merely_flagged(self):
        result = _block(_SHAPES["aws_key"])
        assert _SHAPES["aws_key"] not in str(result["result"])
        assert _SHAPES["aws_key"] not in str(result["formatted_output"])

    def test_replacement_is_truthy(self):
        """A falsy replacement re-opens the envelope's fallback to result."""
        result = _block(_SHAPES["aws_key"])
        assert result["formatted_output"] == BLOCKED_NOTICE
        assert bool(result["formatted_output"])

    def test_status_is_error(self):
        assert _block(_SHAPES["aws_key"])["status"] == AgentStatus.ERROR.value


class TestStructuralTokensSurvive:
    """The other direction: ordinary scorecard content is released byte-identical."""

    def test_a_real_scorecard_passes_the_gate(self):
        assert scan_released_content(_CLEAN_REPORT) is None
        result = PostProcessNode().execute({"result": _CLEAN_REPORT})
        assert result["formatted_output"] == _CLEAN_REPORT

    @pytest.mark.parametrize(
        "token",
        [
            "PO-2026-01-0002",
            "SUP-ALPHA",
            "WIDGET-A",
            "90d",
            "STAR 2026",
            "2026-02-05",
            "58.9",
            "16 day(s) late",
            "sk-1",
            "Bearer",
        ],
    )
    def test_structural_tokens_are_not_mistaken_for_credentials(self, token):
        assert scan_released_content(token) is None
