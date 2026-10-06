"""AgentCore Platform v1.0"""

# RET-C2-158 — DomainWorkflowGraph (inner BaseGraph)
#
# This is the INNER graph for the Cat 2 two-layer nested architecture.
# It encapsulates the full supplier delivery performance domain workflow:
#
#   START → data_ingestion → discrepancy_detection → performance_metrics
#         → report_generation → END
#
# Called by SupplierReportGraphNode.get_subgraph() (graph.py).
# get_output() shapes the sub_result dict consumed by merge_output() there.
#
# Rules enforced:
#   ✅ Inherits BaseGraph (fully custom topology — no forced backbone)
#   ✅ Implements all BaseGraph ABC methods
#   ✅ register_nodes() does NOT call super() (abstract in BaseGraph)
#   ✅ register_nodes() instantiates every domain node with NO constructor args
#   ✅ Does NOT register initialize / finalize (outer backbone concerns)
#   ✅ get_output() designed together with SupplierReportGraphNode.merge_output()
#   ❌ No platform SDK imports

from typing import Any, Dict

from framework.errors import ConfigError
from framework.graph.base_graph import BaseGraph
from framework.schemas.agent_status import AgentStatus
from langgraph.graph import END, START

from src.nodes.data_ingestion_node import DataIngestionNode
from src.nodes.discrepancy_detection_node import DiscrepancyDetectionNode
from src.nodes.performance_metrics_node import PerformanceMetricsNode
from src.nodes.report_generation_node import ReportGenerationNode
from src.schemas.state import State
from src.services.service import parse_domain_settings


class DomainWorkflowGraph(BaseGraph):
    """Inner domain workflow graph for RET-C2-158.

    Inherits BaseGraph directly for a fully custom node topology.
    Called by SupplierReportGraphNode.get_subgraph() in graph.py.

    Pipeline (linear):
        START
          → data_ingestion          (DataIngestionNode)
          → discrepancy_detection   (DiscrepancyDetectionNode)
          → performance_metrics     (PerformanceMetricsNode)
          → report_generation       (ReportGenerationNode)
          → END

    All nodes are FunctionNode subclasses returning partial-dict state updates.
    initialize / finalize are outer backbone concerns — not registered here.
    The runtime tuning arrives through the constructor (config["ret_c2_158"])
    and is seeded into inner state, because node execute() methods take no
    config argument.
    """

    # ── Identity ──────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        """Unique identifier for this inner graph."""
        return "ret_c2_158_supplier_report_workflow"

    @property
    def state_schema(self) -> type:
        """TypedDict subclass shared across inner and outer graph."""
        return State

    # ── Config validation ─────────────────────────────────────────────────────

    def _validate_config(self) -> None:
        """Reject a tuning block that would silently change the scorecard.

        Every value in the ret_c2_158 block is read by a domain node and shapes
        the released report, so a malformed one is refused at compile time
        rather than coerced to a default the operator never declared.
        """
        try:
            self._domain_settings: Dict[str, Any] = parse_domain_settings(self.config.get("ret_c2_158"))
        except ValueError as exc:
            raise ConfigError(f"[{self.__class__.__name__}] {exc}") from exc

    # ── Initial state ─────────────────────────────────────────────────────────

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the validated runtime tuning into inner state.

        The framework hands the inner graph only the request object; node
        execute() methods take no config argument, so state seeding is the only
        route a declared value can reach a domain node.
        """
        settings = getattr(self, "_domain_settings", None)
        if settings is None:
            settings = parse_domain_settings(self.config.get("ret_c2_158"))
        return {"domain_settings": dict(settings)}

    # ── Node registration ─────────────────────────────────────────────────────

    def register_nodes(self) -> None:
        """Register all 4 domain nodes.

        No super() call — BaseGraph.register_nodes() is abstract.
        Do NOT register initialize or finalize; those are outer backbone
        concerns handled by AgentBaseGraph in graph.py.
        Every key registered here is referenced in add_edges().
        """
        self._nodes["data_ingestion"] = DataIngestionNode()
        self._nodes["discrepancy_detection"] = DiscrepancyDetectionNode()
        self._nodes["performance_metrics"] = PerformanceMetricsNode()
        self._nodes["report_generation"] = ReportGenerationNode()

    # ── Edge wiring ───────────────────────────────────────────────────────────

    def add_edges(self) -> None:
        """Wire the linear supplier report domain topology.

        The topology is intentionally linear — no conditional branching
        between domain nodes. A step that fails does not stop the run here;
        instead each downstream step refuses to overwrite an error status, so
        the inner graph reports the failure rather than a scorecard assembled
        from data that never arrived.
        """
        self._sg.add_edge(START, "data_ingestion")
        self._sg.add_edge("data_ingestion", "discrepancy_detection")
        self._sg.add_edge("discrepancy_detection", "performance_metrics")
        self._sg.add_edge("performance_metrics", "report_generation")
        self._sg.add_edge("report_generation", END)

    # ── Routing ───────────────────────────────────────────────────────────────

    def route(self, state: State) -> str:
        """Conditional routing — required by the BaseGraph contract.

        add_conditional_edges() is not used for this linear topology, so this is
        never called at runtime. It is annotated with this graph's own State
        because LangGraph reads a path callable's annotation as its input schema
        and projects away every field the annotation does not declare. Both
        branches return END so an unexpected call can never re-enter a
        mid-graph node.
        """
        if state.get("status") == AgentStatus.ERROR.value:
            return str(END)
        return str(END)

    # ── Output shape ──────────────────────────────────────────────────────────

    def get_output(self, state: State) -> Dict[str, Any]:
        """Shape the output dict returned to the outer graph as sub_result.

        Received by SupplierReportGraphNode.merge_output() in graph.py. Both
        are designed together to guarantee field-name consistency:

            Inner get_output()   emits: "report_markdown", "result", "status",
                                        "report_metadata", "trace_id", ...
            Outer merge_output() reads: sub_result.get("report_markdown"),
                                        sub_result.get("result"),
                                        sub_result.get("status"),
                                        sub_result.get("report_metadata")

        On a non-success status the report is withheld here as well as at the
        outer boundary: a step that failed leaves the state it should have
        written empty, so anything rendered downstream describes data that
        never arrived.
        """
        status = state.get("status")
        if status != AgentStatus.SUCCESS.value:
            return {
                "report_markdown": None,
                "result": None,
                "report_metadata": {},
                "status": status,
                "trace_id": state.get("trace_id"),
                "correlation_id": state.get("correlation_id"),
                "node_history": state.get("node_history", []),
            }
        return {
            "report_markdown": state.get("report_markdown"),
            "result": state.get("result"),
            "report_metadata": state.get("report_metadata", {}),
            "status": status,
            "trace_id": state.get("trace_id"),
            "correlation_id": state.get("correlation_id"),
            "node_history": state.get("node_history", []),
        }
