# Template Design Specification — RET-C2-158

**Template:** Supplier Delivery Performance & Order Discrepancy Report
**Category:** Cat 2 (Domain-Specific DocGen Pipeline)
**Industry:** RET (Retail / Supply Chain)
**Inheritance:** `AgentBaseGraph` (framework base class — direct inheritance)
**Pattern:** two-layer nested (outer backbone + inner domain workflow)

| Role | Class |
|---|---|
| L1 Base (framework base class) | `AgentBaseGraph` — direct framework inheritance |

## 1. Overview

SupplierDeliveryPerformanceReportAgent takes a reporting month and the
purchase-order, delivery, return and invoice records for it, matches every
purchase order against its delivery and invoice (the 3-way match), flags the
discrepancies — quantity shortfalls, late deliveries, invoice price deviations,
defect returns — computes the supplier KPIs (OTD%, fill rate, OTIF%, defect
rate, invoice accuracy) and a weighted composite score, and returns a ranked
**supplier performance scorecard** in Markdown with the high-severity alerts
and the follow-up actions the numbers call for.

The scorecard is computed and rendered deterministically. No model is invoked
anywhere in the pipeline; `generation_mode` is `deterministic`.

## 2. Architecture

```
OUTER backbone (AgentBaseGraph — fixed; add_edges() NOT overridden)
  START -> initialize -> pre_process -> main -> post_process -> finalize -> END
                                         |
                                         v  (SupplierReportGraphNode.get_subgraph)
INNER domain workflow (DomainWorkflowGraph : BaseGraph)
  START -> data_ingestion -> discrepancy_detection -> performance_metrics
        -> report_generation -> END
```

- The `main` slot is **`SupplierReportGraphNode`**, a `GraphNode` subclass. It
  delegates the whole domain workflow to `DomainWorkflowGraph`.
- `extract_input()` hands the inner graph the **validated request contract**
  (period, supplier filter, records) and nothing else; the raw request never
  crosses. Without a contract an empty request is handed over and the
  ingestion step refuses it.
- `merge_output()` maps the inner scorecard into outer state as both
  `report_markdown` and **`result`** — the field the output gate reads and the
  envelope surfaces — and withholds both on a non-success inner status.
- The inner topology is linear. A step that fails does not stop the run;
  every downstream step refuses to overwrite an error status, so the inner
  graph reports the failure rather than a scorecard assembled from data that
  never arrived.

### Directory layout

| Path | Role |
|---|---|
| `src/graph/graph.py` | outer graph, main-slot node, envelope |
| `src/graph/domain_workflow_graph.py` | inner graph: four steps, linear edges |
| `src/services/service.py` | the single definition of the caller contract, the credential union, the runtime-settings parser |
| `src/nodes/pre_process_node.py` | request boundary |
| `src/nodes/post_process_node.py` | output boundary |
| `src/nodes/data_ingestion_node.py` | domain step 1 — publish the records |
| `src/nodes/discrepancy_detection_node.py` | domain step 2 — 3-way match and flags |
| `src/nodes/performance_metrics_node.py` | domain step 3 — KPIs, composite, ranking |
| `src/nodes/report_generation_node.py` | domain step 4 — render the scorecard |
| `src/schemas/state.py` | flat `State(AgentState)` |
| `config/agent.yaml` | static manifest (identity, entry point, requirements) |
| `config/config.yaml` | runtime parameters |
| `src/api/server.py` | HTTP adapter |

## 3. Request contract

Two channels reach the agent.

| Field | Content |
|---|---|
| `input` | the request object as a JSON string: `period` (required, `YYYY-MM`), `supplier_filter` (optional), `output_format` (optional, `markdown`) |
| `input_context` | structured invocation parameters: `purchase_orders`, `delivery_records`, `defect_returns`, `invoice_records`, and optionally `supplier_filter`. **The records belong here.** |

The platform rewrites personal-data shapes out of the request string at every
node boundary, so a supplier identifier that resembles a phone or resident
number (`SUP-123-45-6789`) arrives masked on `input` and is refused as a
non-inert identifier — and arrives intact on `input_context`. Declaring the
filter on both channels is refused as ambiguous.

Rules that hold for every field, defined once in `src/services/service.py` and
enforced by the HTTP adapter and the pre_process node alike:

| Rule | Detail |
|---|---|
| Inert identifiers | `po_id`, `supplier_id`, `line_item`, `status`, `return_reason` and the supplier filter are restricted to `[A-Za-z0-9_-]{1,32}`, anchored with `\A` and `\Z` — `$` also matches before a trailing newline, and these values are rendered into table rows and headings. |
| Finite numbers | `qty_ordered`, `qty_received`, `qty_defective`, `invoiced_qty` are whole numbers in `[0, 10^9]`; `unit_price`, `invoiced_price` are finite numbers in `[0, 10^9]`. `NaN` and the infinities parse through `float()` and compare False against every bound, so they are refused explicitly, whether sent as JSON numbers or bare tokens. Booleans and numeric strings are refused. |
| Strict dates | `YYYY-MM-DD`, anchored, and a real calendar date. |
| Period | `YYYY-MM`, not in the future, within the last 36 months. |
| Closed schema | an undeclared context key or record field is refused, not dropped; a required field that is missing is refused. |
| Referential rules | one purchase order per `po_id`; one delivery and one invoice per order; a delivery, return or invoice must reference a purchase order in the request. Several returns per order are allowed. |
| Structural caps | 500 records per list, 500 identifiers in the filter, 32,768 characters of request string, 256 KB of structured parameters, 200,000 characters of rendered scorecard. |
| Disallowed instructions | chat-template control tokens (`<\|…\|>`, `[INST]`, `<<SYS>>`, role tags) are screened as a class, raw and after markup and invisible characters are stripped, keys included; instruction-shaped phrases require a verb and its object so ordinary procurement text is unaffected. |
| Credential shapes | refused at the adapter and at the request boundary with the framework's own detector as the floor and the local additions on top. A credential-shaped string in `input_context` would otherwise fail the framework's first node with a traceback the caller cannot act on. |
| Refusals | name the field, never repeat the value; a hostile field name is reported by position. |

When the request carries no records, the pipeline degrades to the built-in
sample dataset (three purchase orders, two suppliers) and the scorecard says
so. A deployment configured with `data_source: caller` refuses such a request
instead.

## 4. Node design

| Node | Layer | Responsibility | Writes |
|---|---|---|---|
| `PreProcessNode` | outer pre_process | validate the whole request against the caller contract; refuse with the field named | `validated_input`, `status`; on refusal `formatted_output`, `result` |
| `SupplierReportGraphNode` | outer main | forward the runtime tuning, run the inner workflow, map its scorecard to `result` | `report_markdown`, `result`, `report_metadata`, `status` |
| `DataIngestionNode` | inner 1 | publish the caller's records, or the sample dataset, honouring the supplier filter | the four record lists, `record_source` |
| `DiscrepancyDetectionNode` | inner 2 | 3-way match; raise `qty_shortfall`, `late_delivery`, `invoice_mismatch`, `defect_return` flags with structured measures | `matched_triples`, `discrepancy_flags` |
| `PerformanceMetricsNode` | inner 3 | KPIs, weighted composite, ranking (ties broken on the identifier) | `supplier_kpis`, `supplier_rankings` |
| `ReportGenerationNode` | inner 4 | render the scorecard deterministically; state the data source | `report_markdown`, `result`, `report_metadata` |
| `PostProcessNode` | outer post_process | scan the released surface; on a violation clear every output-bearing field and return a truthy notice | `formatted_output`, `result`, `report_markdown`, `status` |

Every node is a `FunctionNode` subclass returning a partial update, and every
node declares `TrustLevel.VERIFIED_EXTERNAL` — the level the manifest admits.
The inner graph is invoked with the caller's own trust level, so a node
demanding the internal level could not be reached by any caller the entry
point admits and every request would fail at that node's trust gate.

Each node emits one domain audit event on its success path (`emit_trace_event`);
the request boundary additionally emits `request_refused` and the output
boundary `output_withheld`. Payloads carry counts, labels and inert identifiers,
never a caller value.

### Security gate placement

Domain validation runs inside `execute()`; the framework's `@final`
`_security_gate_input` / `_security_gate_output` are never overridden and the
`_extra_security_gate_*` hooks are not used. The platform's own screens run
first on every node; the template's screens then enforce what the platform
does not score (the `<<SYS>>` class, the closed record schema, the finite-number
rule), so the refusal holds on the platform's direct `invoke()` path as well
as on the standalone adapter.

## 5. State

Flat `State(AgentState)` (`src/schemas/state.py`). All values are primitives,
lists and dicts of primitives.

| Field | Producer | Notes |
|---|---|---|
| `validated_input` | PreProcessNode | the validated contract; also the inner graph's request object |
| `domain_settings` | both graphs' initial-state hooks | the validated `ret_c2_158` tuning |
| `purchase_orders`, `delivery_records`, `defect_returns`, `invoice_records` | DataIngestionNode | validated records |
| `record_source` | DataIngestionNode | `caller` or `baseline` |
| `matched_triples`, `discrepancy_flags` | DiscrepancyDetectionNode | joined records; flags with `shortfall` / `days_late` / `price_deviation_pct` / `qty_defective` |
| `supplier_kpis`, `supplier_rankings` | PerformanceMetricsNode | per-supplier KPIs; ranking |
| `report_markdown`, `result`, `report_metadata` | ReportGenerationNode | the scorecard; `{period, generated_at, supplier_count, alert_count, record_source}` |
| `trace_id` / `correlation_id` | framework | tracing only |

No credential, secret or model object is present in state.

## 6. Runtime configuration

`config/config.yaml` carries the live parameters. Every key has a reader.

| Key | Reader | Effect |
|---|---|---|
| `max_retry` | the backbone | retry ceiling; validated at compile time |
| `timeout_s` | the runtime | request timeout |
| `ret_c2_158.data_source` | request boundary, ingestion step | `mock`: a request without records is served from the sample dataset; `caller`: it is refused |
| `ret_c2_158.late_delivery_tolerance_days` | discrepancy step | days late still counted as on time |
| `ret_c2_158.price_tolerance_pct` | discrepancy step | invoice deviation tolerated before a flag |
| `ret_c2_158.kpi_weights` | metrics step | composite-score weights; must sum to 1.0 |

The path a value travels: the registry (or the HTTP adapter, which mirrors it)
loads the file and passes it to the graph constructor; the outer graph
validates the block at compile time (a malformed value refuses to start rather
than being coerced to a default nobody declared), seeds it into the outer
state, and hands it to the main slot, which constructs the inner graph with it;
the inner graph seeds it into its own state. Node `execute()` methods take no
config argument, so state seeding is the only route a declared value can reach
a domain node. The integration suite proves that each tuning value visibly
changes the released scorecard.

## 7. The output boundary

The stated invariant: **nothing credential-shaped leaves the agent, and the
scorecard renders only inert identifiers, validated dates and numbers computed
from validated inputs.**

The agent renders no monetary aggregates. The scorecard carries KPI
percentages, composite scores, counts, day counts and a price *deviation*
percentage — unit prices are commercially sensitive and are not rendered — so
a monetary rounding grid does not apply. The identifier invariant is enforced
at the request boundary, where the alphabet is closed before anything is
rendered; the credential invariant is enforced here, on every representation:
the rendered scorecard and the structured metadata behind it, nested values
included.

The credential half takes the **union** of the platform's own detector and the
local patterns. The platform scans every value of every node result and RAISES
when it finds a credential; the wrapper then discards the gate node's whole
return value — the clearing included — and the envelope falls back to the
un-gated scorecard still in state. A local list narrower than the platform's is
therefore a containment bypass; and the platform's patterns describe credential
formats and match none of `password=…`, `pk-…`, a short `sk-…` key, a bearer
value carrying `~+/`, or a private-key block, so those are kept as local
additions.

### Containment

`AgentBaseGraph.get_output` resolves the released output as
`formatted_output or result`, with no status check. Three consequences, all
handled:

1. A falsy `formatted_output` re-opens the fallback, so the withheld notice is
   truthy.
2. A gate that raises leaks, because the wrapper turns an exception into a bare
   error update that clears nothing — which is why the gate's detector is a
   superset of the platform's rather than narrower than it.
3. The platform's detector moves between releases, so `get_output` is
   overridden as well: on any non-success status the output resolves to the
   gate's notice or to None, never to `result`. The integration suite proves
   this layer with the gate's own scan disabled.

No third layer re-scans the success path. One would contain a leak by itself
and thereby make the gate node's own scan unfalsifiable.

## 8. Design Decision Record

| Decision | Choice | Rationale |
|---|---|---|
| Base class | `AgentBaseGraph` (direct) | the framework base class every template inherits |
| Cat classification | Cat 2 (nested) | multi-step domain workflow; retail supply chain specific |
| Inner graph composition | `DomainWorkflowGraph(BaseGraph)` | GraphNode in the main slot; domain complexity encapsulated inside |
| KPI calculation and rendering | pure Python, deterministic | auditable; the same input always yields the same scorecard; no model dependency |
| Records channel | `input_context` | the platform masks personal-data shapes out of the request string; the structured channel is not rewritten |
| 3-way match join key | `po_id` | unique per PO line; appears in all four record types; one delivery and one invoice per order by contract |
| Defect quantity | structured `qty_defective` on the `defect_return` flag | the metrics step reads the field, never a rendered sentence |
| Price discrepancies | deviation percentage only | unit prices are commercially sensitive and are not rendered |
| Trust levels | `VERIFIED_EXTERNAL` on every node | the level the manifest admits; the inner graph runs at the caller's own level |
| Absent records | sample dataset, labelled | a demonstrable baseline; `data_source: caller` turns it off for production |
