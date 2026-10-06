# RET-C2-158 — Unit Tests: Graph (outer graph composition, extract_input/merge_output, envelope)
#
# Tests:
#   - RetC2158Agent instantiates and compiles
#   - register_nodes() fills pre_process, main, post_process slots and hands the tuning to the main slot
#   - SupplierReportGraphNode.extract_input() hands over the validated contract and nothing else
#   - SupplierReportGraphNode.merge_output() maps report_markdown → result, withholds on non-success
#   - get_output() never falls back to `result` on a non-success status

from framework.schemas.agent_status import AgentStatus

from src.graph.domain_workflow_graph import DomainWorkflowGraph
from src.graph.graph import RetC2158Agent, SupplierReportGraphNode
from src.schemas.state import State


class TestRetC2158AgentInstantiation:
    def test_agent_instantiates_and_compiles(self):
        agent = RetC2158Agent()
        agent.compile()
        assert agent._compiled is not None

    def test_agent_name_property(self):
        assert RetC2158Agent().name == "ret_c2_158"

    def test_state_schema_is_state(self):
        assert RetC2158Agent().state_schema is State

    def test_register_nodes_fills_the_template_slots(self):
        agent = RetC2158Agent(config={"ret_c2_158": {"late_delivery_tolerance_days": 3}})
        agent.register_nodes()
        assert type(agent._nodes["pre_process"]).__name__ == "PreProcessNode"
        assert type(agent._nodes["post_process"]).__name__ == "PostProcessNode"
        main = agent._nodes["main"]
        assert isinstance(main, SupplierReportGraphNode)
        assert main.domain_settings["late_delivery_tolerance_days"] == 3


class TestSupplierReportGraphNodeMethods:
    def test_extract_input_hands_over_the_validated_contract(self):
        validated = {"period": "2026-01", "supplier_filter": ["SUP-001"], "output_format": "markdown", "records": None}
        extracted = SupplierReportGraphNode().extract_input({"validated_input": validated, "user_input": "raw"})
        assert extracted == validated

    def test_extract_input_fails_closed_without_a_contract(self):
        """The raw request never crosses: without the contract an empty request is handed over."""
        extracted = SupplierReportGraphNode().extract_input({"user_input": '{"period": "2026-02"}'})
        assert extracted == {}

    def test_merge_output_maps_report_markdown_to_result(self):
        sub_result = {
            "report_markdown": "# Report",
            "result": "# Report",
            "status": "success",
            "report_metadata": {"period": "2026-01"},
        }
        merged = SupplierReportGraphNode().merge_output({}, sub_result)
        assert merged == {
            "report_markdown": "# Report",
            "result": "# Report",
            "status": "success",
            "report_metadata": {"period": "2026-01"},
        }

    def test_merge_output_withholds_on_a_non_success_status(self):
        merged = SupplierReportGraphNode().merge_output(
            {}, {"report_markdown": "# leaked", "result": "# leaked", "status": "error"}
        )
        assert merged["result"] is None and merged["report_markdown"] is None

    def test_merge_output_returns_only_changed_keys(self):
        merged = SupplierReportGraphNode().merge_output(
            {"user_input": "x", "session_id": "s"}, {"report_markdown": "# R", "result": "# R", "status": "success"}
        )
        assert "user_input" not in merged and "session_id" not in merged

    def test_subgraph_carries_the_tuning(self):
        node = SupplierReportGraphNode()
        node.domain_settings = dict(node.domain_settings, price_tolerance_pct=0.5)
        assert node.get_subgraph().config["ret_c2_158"]["price_tolerance_pct"] == 0.5


class TestEnvelope:
    def test_success_releases_the_report(self):
        out = RetC2158Agent().get_output({"status": "success", "formatted_output": "# R", "result": "# R"})
        assert out["output"] == "# R"

    def test_error_never_falls_back_to_result(self):
        out = RetC2158Agent().get_output({"status": "error", "result": "# un-gated report"})
        assert out["output"] is None

    def test_error_surfaces_the_notice_when_one_exists(self):
        out = RetC2158Agent().get_output(
            {"status": "error", "formatted_output": "notice", "result": "# un-gated report"}
        )
        assert out["output"] == "notice"


class TestDomainWorkflowGraphInstantiation:
    def test_domain_workflow_graph_instantiates_and_compiles(self):
        graph = DomainWorkflowGraph()
        graph.compile()
        assert graph.name == "ret_c2_158_supplier_report_workflow"

    def test_route_is_annotated_with_the_graphs_own_state(self):
        assert DomainWorkflowGraph.route.__annotations__["state"] is State

    def test_get_output_withholds_on_non_success(self):
        out = DomainWorkflowGraph().get_output(
            {"status": AgentStatus.ERROR.value, "result": "# leaked", "report_markdown": "# leaked"}
        )
        assert out["result"] is None and out["report_markdown"] is None
