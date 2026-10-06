# RET-C2-158 — output containment at the gate node.
#
# The framework's envelope builder returns
#
#     {"output": state["formatted_output"] or state["result"], ...}
#
# and it returns it on an ERROR status too. That single line is why "the gate
# refused" and "the caller did not see it" are different claims, and why these
# tests assert the SECOND one.
#
# A gate that reports an error and leaves `result` in place ships the text it
# just refused, inside the error envelope. So every refusal must do three things
# together — report the error, CLEAR every output-bearing field, and publish a
# NON-EMPTY notice — and each of the three is asserted separately below, because
# any two of them still leak.
#
# The final test in the containment class reproduces what the PREVIOUS gate
# returned on the same input and shows the envelope carrying the refused text.
# It is here so the fix is falsifiable rather than merely present.

import pytest
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import PostProcessNode

_CLEAN_REPORT = "# Supplier Performance Scorecard — 2026-01\n\n| SUP-001 | 92.0 |\n"

# One example per credential family the gate must withhold. The first six are
# shapes the framework's own detector carries; the rest are the local additions.
_CREDENTIALS = [
    "AKIAIOSFODNN7EXAMPLE",
    "sk_live_" + "TESTKEY1234567890abcd",
    "sk-TESTKEY1234567890abcdefghij",
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c",
    "Bearer AAAABBBBCCCCDDDDEEEEFFFF00001111",
    "postgresql://db.internal.example:5432/procurement",
    "password=hunter2hunter2",
    "api_key: 8f2b7c1d9e4a6b3c",
    "pk-TESTKEY1234567890abcdef",
    "Bearer abc123def456",
    "-----BEGIN RSA PRIVATE KEY-----",
]


def _envelope(state, delta):
    """Reproduce the framework's own get_output fallback over a node delta."""
    merged = dict(state)
    merged.update(delta)
    return merged.get("formatted_output") or merged.get("result")


class TestTrustLevel:
    def test_post_process_node_trust_level(self):
        assert PostProcessNode.required_trust_level == TrustLevel.VERIFIED_EXTERNAL


class TestRelease:
    def test_clean_report_is_released_unchanged(self):
        delta = PostProcessNode().execute({"result": _CLEAN_REPORT, "report_metadata": {"period": "2026-01"}})
        assert delta["status"] == AgentStatus.SUCCESS.value
        assert delta["formatted_output"] == _CLEAN_REPORT
        assert delta["result"] == _CLEAN_REPORT


class TestContainment:
    @pytest.mark.parametrize("credential", _CREDENTIALS)
    def test_refused_report_is_withheld_from_the_envelope(self, credential):
        state = {"result": f"{_CLEAN_REPORT}\n{credential}\n", "report_markdown": f"{_CLEAN_REPORT}\n{credential}\n"}
        delta = PostProcessNode().execute(state)
        assert delta["status"] == AgentStatus.ERROR.value
        assert credential not in str(_envelope(state, delta))

    @pytest.mark.parametrize("credential", _CREDENTIALS)
    def test_all_three_halves_of_containment(self, credential):
        """Report, clear, and publish a truthy notice — any two of the three still leak."""
        delta = PostProcessNode().execute({"result": f"{_CLEAN_REPORT}\n{credential}"})
        assert delta["status"] == AgentStatus.ERROR.value, "1/3: the error is reported"
        for field in ("result", "formatted_output", "report_markdown", "llm_response"):
            assert (
                field in delta
            ), f"2/3: {field} must be PRESENT in the update — omission leaves the old value in state"
            assert credential not in str(delta[field])
        assert delta["report_markdown"] is None
        assert delta["formatted_output"], "3/3: a FALSY notice re-opens the fallback"

    def test_the_notice_carries_no_detail_about_what_was_withheld(self):
        secret = "AKIAIOSFODNN7EXAMPLE"
        delta = PostProcessNode().execute({"result": f"{_CLEAN_REPORT}\n{secret}"})
        assert secret not in delta["formatted_output"]
        assert "AKIA" not in delta["formatted_output"]
        assert "Traceback" not in delta["formatted_output"]

    def test_the_error_log_names_the_rule_not_the_text(self):
        secret = "password=hunter2hunter2"
        delta = PostProcessNode().execute({"result": f"{_CLEAN_REPORT}\n{secret}"})
        joined = " ".join(delta["error_log"])
        assert "credential_assignment" in joined
        assert secret not in joined

    def test_a_leak_in_the_metadata_is_withheld_too(self):
        delta = PostProcessNode().execute(
            {"result": _CLEAN_REPORT, "report_metadata": {"note": {"nested": "AKIAIOSFODNN7EXAMPLE"}}}
        )
        assert delta["status"] == AgentStatus.ERROR.value
        assert "AKIA" not in str(delta)

    @pytest.mark.parametrize("value", [None, "", "   ", 0, [], {}])
    def test_an_empty_report_is_refused_and_still_publishes_the_notice(self, value):
        delta = PostProcessNode().execute({"result": value})
        assert delta["status"] == AgentStatus.ERROR.value
        assert delta["formatted_output"]
        assert delta["result"] == delta["formatted_output"]

    def test_an_oversized_report_is_refused(self):
        delta = PostProcessNode().execute({"result": "x" * 200_001})
        assert delta["status"] == AgentStatus.ERROR.value
        assert "output_too_large" in " ".join(delta["error_log"])
        assert len(delta["formatted_output"]) < 200

    def test_the_previous_gate_leaked_the_same_input(self):
        """The fix is falsifiable: this is what the shipped code returned.

        The earlier gate scanned only its own four patterns and, on a miss,
        returned neither `result` nor `formatted_output` when the framework
        raised — so the framework's fallback served the refused text.
        Reproduced here rather than described, so that a future revision which
        drops the clearing has something concrete to fail against.
        """
        secret = "AKIAIOSFODNN7EXAMPLE"
        state = {"result": f"{_CLEAN_REPORT}\n{secret}"}
        previous_shape = {
            "status": AgentStatus.ERROR.value,
            "error_log": ["[PostProcessNode] S-3 output gate: credential pattern 'aws_key' detected"],
        }
        assert secret in _envelope(
            state, previous_shape
        ), "fixture check: the previous return shape must be the leaking one"
        current = PostProcessNode().execute(state)
        assert secret not in str(_envelope(state, current))
