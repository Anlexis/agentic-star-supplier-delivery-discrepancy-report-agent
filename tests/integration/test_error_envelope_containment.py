# Containment of the error envelope, driven end to end.
#
# The envelope resolves the released output as `formatted_output or result`,
# with no status check. Three properties follow, and all three are live here:
#
#   1. a falsy formatted_output ACTIVATES the fallback, so "" as a "withheld"
#      marker produces the exact disclosure it was written to prevent;
#   2. an output gate that RAISES leaks, because the wrapper turns an exception
#      into a bare error update that clears nothing;
#   3. the platform's own credential scan raises on the gate node's return
#      value and the wrapper then discards that node's whole update — the
#      clearing included — so a gate whose pattern set is narrower than the
#      platform's is a containment bypass rather than a weaker filter.
#
# The fault is injected on the DATA path, never on the gate: the main slot's
# merge_output is made to publish a drifted scorecard, exactly as a record
# carrying a credential-shaped value would if it reached the renderer. Patching
# the gate would test the patch, not the agent.
#
# Two layers are proven separately, because each masks the other:
#   * the gate node's clearing — the drifted scorecard is refused by the gate
#     and the output is the gate's own notice;
#   * the envelope override — when the local scan is made to MISS a shape the
#     platform catches (the release-drift case), the platform raises inside the
#     gate node, the clearing is discarded, and the override alone keeps the
#     un-gated scorecard out of the envelope.
#
# Deterministic — no model, no network.

import json

import pytest

from tests.integration.asgi import post

from src.api.server import app
from src.graph.graph import SupplierReportGraphNode
from src.nodes import post_process_node
from src.nodes.post_process_node import BLOCKED_NOTICE

_TOKEN = "containment-caller-token"
AUTH = {"Authorization": f"Bearer {_TOKEN}"}
_REQUEST = {"input": '{"period": "2026-01"}'}


@pytest.fixture(autouse=True)
def _caller_token(monkeypatch):
    """The deployment shape under test: the bearer credential is configured.

    Set per test rather than at import time — every test module is imported
    before any test runs, so a module-level assignment is overwritten by
    whichever module happens to be imported last.
    """
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
    monkeypatch.delenv("STG_INTERNAL_RUNNER_TOKEN", raising=False)


# A shape the platform's own detector recognises. Probing with one it does not
# would report the leak as contained when it is not.
_LEAKED = "AKIAIOSFODNN7EXAMPLE"
_DRIFTED_REPORT = f"# Supplier Performance Scorecard — 2026-01\n\nupstream note: {_LEAKED}\n"


@pytest.fixture
def drifted_main_slot(monkeypatch):
    """Make the DATA path publish a scorecard the gate must refuse."""

    def _drifted(self, state, sub_result):  # noqa: ANN001
        return {
            "report_markdown": _DRIFTED_REPORT,
            "result": _DRIFTED_REPORT,
            "status": sub_result.get("status"),
            "report_metadata": sub_result.get("report_metadata") or {},
        }

    monkeypatch.setattr(SupplierReportGraphNode, "merge_output", _drifted)
    return _drifted


@pytest.fixture
def local_scan_blind(monkeypatch):
    """Simulate the release-drift case: the local scan misses what the platform catches."""
    monkeypatch.setattr(post_process_node, "scan_released_content", lambda content: None)


def _invoke():
    return post(app, _REQUEST, AUTH)


class TestDriftedScorecardIsContainedByTheGate:
    def test_envelope_carries_no_leaked_value(self, drifted_main_slot):
        assert _LEAKED not in json.dumps(_invoke()[1])

    def test_status_is_error(self, drifted_main_slot):
        assert _invoke()[1]["status"] == "error"

    def test_output_is_the_withheld_notice(self, drifted_main_slot):
        """The notice is TRUTHY, so the fallback to result stays closed.

        It also proves the block came from the gate rather than from the
        platform raising: when the platform raises, the gate node's update is
        discarded and there is no notice to observe.
        """
        assert _invoke()[1]["output"] == BLOCKED_NOTICE

    def test_block_happened_at_the_gate(self, drifted_main_slot):
        assert "PostProcessNode" in _invoke()[1]["node_history"]

    def test_envelope_carries_no_traceback_or_source_path(self, drifted_main_slot):
        rendered = json.dumps(_invoke()[1])
        assert "Traceback" not in rendered
        assert "/src/" not in rendered


class TestPlatformRaisePathIsContainedByTheEnvelope:
    """The second layer, proven with the first one disabled."""

    def test_envelope_carries_no_leaked_value(self, drifted_main_slot, local_scan_blind):
        _, envelope = _invoke()
        assert envelope["status"] == "error"
        assert _LEAKED not in json.dumps(envelope)

    def test_output_is_withheld_not_the_notice(self, drifted_main_slot, local_scan_blind):
        """No notice exists on this path: the gate node's update was discarded.

        The output resolves to None instead of falling back to the un-gated
        scorecard still sitting in state — which is exactly what the envelope
        override exists to guarantee.
        """
        assert _invoke()[1]["output"] is None

    def test_the_fault_really_reached_the_gate_node(self, drifted_main_slot, local_scan_blind):
        assert "PostProcessNode" in _invoke()[1]["node_history"]


class TestCleanPathControl:
    """Without the drift the same request still produces its real answer.

    A gate that refused everything would pass every assertion above; this is
    what stops that from counting as containment.
    """

    def test_same_request_succeeds_when_the_data_path_is_clean(self):
        status, envelope = _invoke()
        assert status == 200
        assert envelope["status"] == "success"
        assert "# Supplier Performance Scorecard — 2026-01" in envelope["output"]

    def test_gate_node_runs_on_the_clean_path_too(self):
        assert "PostProcessNode" in _invoke()[1]["node_history"]
