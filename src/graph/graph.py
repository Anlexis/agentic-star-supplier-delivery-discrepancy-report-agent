"""AgentCore Platform v1.0"""

# RET-C2-158 — Outer graph (AgentBaseGraph; Cat 2 two-layer nested architecture)
#
# Architecture (Cat 2):
#
#   Outer backbone (fixed — identical to Cat 1, do NOT override add_edges()):
#     START → initialize → pre_process → main → {route} → post_process → finalize → END
#                                             ↓ (RETRY, max 3)
#                                          pre_process
#
#   `main` slot is a GraphNode subclass (SupplierReportGraphNode) that delegates
#   the full domain workflow to DomainWorkflowGraph (inner BaseGraph).
#
#   Domain complexity is fully encapsulated inside the inner graph. The outer
#   backbone is never modified.
#
# Directory layout:
#   src/graph/graph.py                 ← outer graph (this file)
#   src/graph/domain_workflow_graph.py ← inner graph (multi-step topology)
#
# Rules enforced:
#   ✅ RetC2158Agent inherits AgentBaseGraph
#   ✅ super().register_nodes() called first (fills initialize + finalize)
#   ✅ SupplierReportGraphNode assigned to self._nodes["main"]
#   ✅ merge_output() returns only changed keys
#   ❌ add_edges() NOT overridden on the outer graph
#   ❌ No platform SDK imports
#
# Runtime configuration: config/config.yaml is loaded by the registry (and by
# the standalone adapter, which mirrors it) and passed as Graph(config=...).
# The ret_c2_158 block is validated here at compile time, handed to the main
# slot, forwarded to the inner graph through its constructor, and seeded into
# state by both graphs — node execute() methods take no config argument, so
# state seeding is the only route a declared value can reach a node.

from typing import Any, ClassVar, Dict, Mapping

from framework.errors import ConfigError
from framework.graph.agent_base_graph import AgentBaseGraph
from framework.nodes.graph_node import GraphNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus

from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.schemas.state import State
from src.services.service import parse_domain_settings


class SupplierReportGraphNode(GraphNode):
    """GraphNode subclass assigned to the `main` slot of RetC2158Agent.

    Wraps DomainWorkflowGraph (inner Cat 2 BaseGraph).
    Called by AgentBaseGraph backbone after pre_process and before post_process.

    Contracts:
      get_subgraph()    — instantiate DomainWorkflowGraph with the forwarded
                          runtime tuning
      extract_input()   — hand the validated request contract to the inner graph
      merge_output()    — map sub_result fields into outer state delta (changed keys only)
      error_strategy    — "propagate": re-raise inner errors as SubgraphError (fail-fast)
    """

    # "propagate": re-raise inner graph exceptions as SubgraphError (default — fail fast).
    # "handle": call on_subgraph_error() instead — use for graceful degradation.
    error_strategy: ClassVar[str] = "propagate"

    # False: HITL interrupts are handled inside the inner graph only.
    # True: surface inner HITL interrupt to the outer caller.
    propagate_hitl: ClassVar[bool] = False

    def __init__(self) -> None:
        # The shipped defaults until the outer graph hands over the validated
        # tuning in register_nodes(). Constructed with no arguments so the
        # boundary suite can instantiate every node bare.
        self.domain_settings: Dict[str, Any] = parse_domain_settings(None)

    def get_subgraph(self) -> Any:
        """Instantiate and return the inner domain workflow graph.

        DomainWorkflowGraph is imported lazily (inside the method) to avoid
        circular-import risk at module load time. The inner graph receives the
        validated runtime tuning through its constructor and seeds it into its
        own state; its domain nodes still take no constructor arguments.
        """
        from src.graph.domain_workflow_graph import DomainWorkflowGraph

        return DomainWorkflowGraph(config={"ret_c2_158": dict(self.domain_settings)})

    def extract_input(self, state: AgentState) -> Any:
        """Return the request object passed into inner_graph.invoke().

        PreProcessNode validates the raw request and the records and writes the
        contract to validated_input. Only that contract crosses into the inner
        graph; the raw request never does. When it is absent — which can only
        happen if the pre_process slot did not run — an empty request is
        handed over and the ingestion step refuses it.
        """
        validated = state.get("validated_input")
        return dict(validated) if isinstance(validated, Mapping) else {}

    def merge_output(self, state: AgentState, sub_result: Dict[str, Any]) -> Dict[str, Any]:
        """Map inner graph sub_result back into the outer state delta.

        sub_result is the dict returned by DomainWorkflowGraph.get_output().
        Returns ONLY changed keys — never the full state.

        Key coupling (designed together with DomainWorkflowGraph.get_output()):
          Inner get_output() emits  → "report_markdown", "result", "status",
                                      "report_metadata", "trace_id", ...
          This merge_output() reads → sub_result.get("report_markdown"),
                                      sub_result.get("result"),
                                      sub_result.get("status"),
                                      sub_result.get("report_metadata")

        The report is mapped to "result" as well, because PostProcessNode (the
        output gate) reads state["result"] — without that mapping the surfaced
        output would always be empty.

        A non-success inner status never reaches here (error_strategy is
        "propagate", so the inner error is re-raised first), but the guard is
        kept so a future strategy change cannot start publishing an un-gated
        report through this path.
        """
        status = sub_result.get("status")
        if status != AgentStatus.SUCCESS.value:
            return {
                "report_markdown": None,
                "result": None,
                "status": status,
                "report_metadata": {},
            }
        report = sub_result.get("report_markdown")
        return {
            "report_markdown": report,
            "result": sub_result.get("result") or report,
            "status": status,
            "report_metadata": sub_result.get("report_metadata") or {},
        }


class RetC2158Agent(AgentBaseGraph):
    """Outer graph for RET-C2-158 (Cat 2).

    Inherits AgentBaseGraph directly (L1 Base). Domain logic is fully
    encapsulated in SupplierReportGraphNode (main slot), which delegates to
    DomainWorkflowGraph (inner BaseGraph).

    Backbone (fixed — identical to Cat 1):
        START → initialize → pre_process → main → post_process → finalize → END

    register_nodes() fills the three template-owned slots:
      - super().register_nodes() fills: initialize, finalize (framework defaults)
      - pre_process: PreProcessNode (S-1 input validation — the caller boundary)
      - main:        SupplierReportGraphNode (delegates to DomainWorkflowGraph)
      - post_process: PostProcessNode (S-3 output gate — the output boundary)

    add_edges() is NOT overridden — backbone wiring belongs to the framework.
    """

    @property
    def name(self) -> str:
        """Agent identifier registered with AgentRegistry."""
        return "ret_c2_158"

    @property
    def state_schema(self) -> type:
        return State

    @property
    def domain_settings(self) -> Dict[str, Any]:
        """The validated ret_c2_158 tuning block from the runtime configuration.

        Parsed on demand from the constructor config and cached, so the same
        validated values reach the pre_process slot (through the initial state)
        and the inner graph (through the main slot).
        """
        cached = getattr(self, "_domain_settings", None)
        if cached is None:
            cached = parse_domain_settings(self.config.get("ret_c2_158"))
            self._domain_settings: Dict[str, Any] = cached
        return dict(cached)

    def _validate_config(self) -> None:
        """Validate the backbone keys, then the ret_c2_158 tuning block.

        A malformed tuning value refuses to compile rather than being coerced
        to a default nobody declared.
        """
        super()._validate_config()
        try:
            self._domain_settings = parse_domain_settings(self.config.get("ret_c2_158"))
        except ValueError as exc:
            raise ConfigError(f"[{self.__class__.__name__}] {exc}") from exc

    def _extra_initial_state(self) -> Dict[str, Any]:
        """Seed the validated tuning into the outer state.

        The pre_process slot reads data_source from it to decide whether a
        request without records is refused or served from the sample dataset.
        """
        return {"domain_settings": self.domain_settings}

    def register_nodes(self) -> None:
        """Fill all 5 backbone slots.

        super().register_nodes() MUST be called first — it injects the
        framework's default InitializeNode (sets schema_version, session_id,
        trust_level) and FinalizeNode (builds response_metadata, total_time_ms).
        """
        super().register_nodes()  # fills: initialize, finalize

        main = SupplierReportGraphNode()
        main.domain_settings = self.domain_settings

        self._nodes["pre_process"] = PreProcessNode()
        self._nodes["main"] = main
        self._nodes["post_process"] = PostProcessNode()

    # add_edges() is NOT overridden — backbone wiring belongs to the framework.

    def get_output(self, state: AgentState) -> Dict[str, Any]:
        """Shape the caller-facing envelope.

        The framework's envelope resolves the output as
        ``formatted_output or result`` with NO status check, so an error path
        that left ``result`` in place would ship the un-gated scorecard inside
        the error envelope. The output gate clears ``result`` when it refuses,
        which closes that path for every shape the gate's own scan recognises.
        It cannot close the path where the framework's own credential scan
        raises on the gate node's return value: the wrapper then discards that
        node's whole delta — the clearing included — and ``result`` survives
        untouched in state. The gate's pattern set is a superset of the
        installed framework's, but the framework's set moves between releases,
        so this override holds the envelope closed on that path too: on any
        non-success status the output resolves to the gate's own notice, or to
        None. It never falls back to ``result``.

        No third layer re-scans the success path here. One would contain a leak
        by itself and thereby make the gate node's own scan unfalsifiable; the
        gate is the single place the success path is checked.
        """
        output: Dict[str, Any] = dict(super().get_output(state))
        if state.get("status") != AgentStatus.SUCCESS.value:
            output["output"] = state.get("formatted_output") or None
        return output
