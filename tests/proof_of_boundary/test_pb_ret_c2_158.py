# RET-C2-158 — Proof-of-Boundary Tests (PB-1, PB-2, PB-3, PB-5)
#
# PB-1: BaseNode → EventEmitter: emit_trace_event() audit path is wired on every
#       domain node and fires on the success path (S-4 audit trail)
# PB-2: State serialization: node output fields are JSON-serializable primitives
# PB-3: the data-source seam: the ingestion node publishes records from both
#       sources the deployment allows, and refuses the third case
# PB-5: Checkpoint safety: no JWT/Pydantic objects in serialized State
#
# PB-4 (import isolation) lives in test_import_isolation.py; PB-6 (invoke order)
# in test_pb_invoke_order.py.

import importlib
import json
import re

import pytest
from framework.schemas.agent_status import AgentStatus

_PO = {"po_id": "PO-1", "supplier_id": "SUP-1", "qty_ordered": 100, "planned_delivery_date": "2026-01-15"}


def _validated_input(period="2026-01", records=None):
    return {
        "period": period,
        "supplier_filter": None,
        "output_format": "markdown",
        "records": records,
        "record_source": "caller" if records is not None else "baseline",
    }


# (node module path, class, expected S-4 event name, state driving the success path)
_S4_NODE_CASES = [
    ("src.nodes.pre_process_node", "PreProcessNode", "pre_process_validated", {"user_input": '{"period": "2026-01"}'}),
    (
        "src.nodes.data_ingestion_node",
        "DataIngestionNode",
        "data_ingestion_complete",
        {"validated_input": _validated_input()},
    ),
    (
        "src.nodes.discrepancy_detection_node",
        "DiscrepancyDetectionNode",
        "discrepancy_detection_complete",
        {"purchase_orders": [], "delivery_records": []},
    ),
    (
        "src.nodes.performance_metrics_node",
        "PerformanceMetricsNode",
        "performance_metrics_calculated",
        {"matched_triples": [], "discrepancy_flags": []},
    ),
    (
        "src.nodes.report_generation_node",
        "ReportGenerationNode",
        "report_generation_complete",
        {
            "supplier_kpis": {},
            "supplier_rankings": [],
            "discrepancy_flags": [],
            "validated_input": {"period": "2026-01"},
        },
    ),
    (
        "src.nodes.post_process_node",
        "PostProcessNode",
        "post_process_complete",
        {"result": "# Supplier Scorecard\n\nclean report body"},
    ),
]


class TestPB1AuditTrailEmitTraceEvent:
    """PB-1: every domain node emits its S-4 audit event on the success path."""

    @pytest.mark.parametrize(
        "module_path, class_name, event_name, state", _S4_NODE_CASES, ids=[c[1] for c in _S4_NODE_CASES]
    )
    def test_node_emits_s4_audit_event(self, monkeypatch, module_path, class_name, event_name, state):
        module = importlib.import_module(module_path)
        calls = []
        monkeypatch.setattr(module, "emit_trace_event", lambda event, payload, st: calls.append((event, payload)))

        result = getattr(module, class_name)().execute(dict(state))

        assert result["status"] == AgentStatus.SUCCESS.value, result
        assert [c[0] for c in calls] == [event_name]
        assert isinstance(calls[0][1], dict)

    def test_refusal_path_emits_its_own_event(self, monkeypatch):
        module = importlib.import_module("src.nodes.pre_process_node")
        calls = []
        monkeypatch.setattr(module, "emit_trace_event", lambda event, payload, st: calls.append((event, payload)))
        module.PreProcessNode().execute({"user_input": ""})
        assert [c[0] for c in calls] == ["request_refused"]
        assert calls[0][1] == {"field": "input", "reason": "is empty"}


class TestPB2StateSerializationSafety:
    """PB-2: node output fields must be JSON-serializable (msgpack-compatible)."""

    def test_full_inner_pipeline_output_is_json_serializable(self):
        from src.nodes.data_ingestion_node import DataIngestionNode
        from src.nodes.discrepancy_detection_node import DiscrepancyDetectionNode
        from src.nodes.performance_metrics_node import PerformanceMetricsNode
        from src.nodes.report_generation_node import ReportGenerationNode

        state = {"validated_input": _validated_input()}
        for node in (DataIngestionNode(), DiscrepancyDetectionNode(), PerformanceMetricsNode(), ReportGenerationNode()):
            delta = node.execute(dict(state))
            json.dumps(delta)
            state.update(delta)
        assert state["report_markdown"]


class TestPB3DataSourceSeam:
    """PB-3: the ingestion node's data-source seam is exercised in every configuration."""

    def test_sample_source_returns_records(self):
        from src.nodes.data_ingestion_node import DataIngestionNode

        result = DataIngestionNode().execute(
            {"validated_input": _validated_input(), "domain_settings": {"data_source": "mock"}}
        )
        assert result["status"] == AgentStatus.SUCCESS.value
        assert result["purchase_orders"]

    def test_caller_records_are_published(self):
        from src.nodes.data_ingestion_node import DataIngestionNode

        result = DataIngestionNode().execute(
            {"validated_input": _validated_input(records={"purchase_orders": [dict(_PO)]})}
        )
        assert result["purchase_orders"] == [_PO]

    def test_caller_only_source_refuses_without_records(self):
        from src.nodes.data_ingestion_node import DataIngestionNode

        result = DataIngestionNode().execute(
            {"validated_input": _validated_input(), "domain_settings": {"data_source": "caller"}}
        )
        assert result["status"] == AgentStatus.ERROR.value


class TestPB5CheckpointSafety:
    """PB-5: serialized State fields must not contain Pydantic or JWT-like objects."""

    def _walk(self, obj, path, check):
        check(obj, path)
        if isinstance(obj, dict):
            for k, v in obj.items():
                self._walk(v, f"{path}.{k}", check)
        elif isinstance(obj, (list, tuple)):
            for i, item in enumerate(obj):
                self._walk(item, f"{path}[{i}]", check)

    def test_no_pydantic_objects_or_jwt_strings_in_state_output(self):
        from pydantic import BaseModel as PydanticBase

        from src.nodes.data_ingestion_node import DataIngestionNode

        jwt_pattern = re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")
        result = DataIngestionNode().execute({"validated_input": _validated_input()})

        def check(obj, path):
            assert not isinstance(obj, PydanticBase), f"Pydantic object at {path}"
            if isinstance(obj, str):
                assert not jwt_pattern.search(obj), f"JWT-like string at {path}"

        self._walk(result, "root", check)
