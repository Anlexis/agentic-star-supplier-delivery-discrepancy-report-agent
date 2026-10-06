# Test Specification — RET-C2-158

**Template:** Supplier Delivery Performance & Order Discrepancy Report
**Category:** Cat 2 (two-layer nested) · **Inheritance:** `AgentBaseGraph`

The tables below describe the tests that ship in this repository. Every case is
deterministic: no model call, no network.

## 1. Test files

| Layer | Target | File |
|---|---|---|
| Unit | the caller contract (all accepted and refused shapes) | `tests/unit/test_caller_contract.py` |
| Unit | the runtime tuning block and its path into both graphs | `tests/unit/test_domain_settings.py` |
| Unit | `PreProcessNode` (the caller boundary) | `tests/unit/test_pre_process_node.py` |
| Unit | `DataIngestionNode` | `tests/unit/test_data_ingestion_node.py` |
| Unit | `DiscrepancyDetectionNode` | `tests/unit/test_discrepancy_detection_node.py` |
| Unit | `PerformanceMetricsNode` | `tests/unit/test_performance_metrics_node.py` |
| Unit | `ReportGenerationNode` | `tests/unit/test_report_generation_node.py` |
| Unit | `PostProcessNode` containment | `tests/unit/test_post_process_node.py` |
| Unit | graph composition, config plumbing, envelope resolution | `tests/unit/test_graph.py` |
| Unit | state schema | `tests/unit/test_state.py` |
| Unit | framework compliance | `tests/unit/test_framework_compliance_tc06_tc07.py` |
| Boundary | the request boundary refuses on its own account | `tests/proof_of_boundary/test_request_boundary.py` |
| Boundary | the output boundary and its clearing | `tests/proof_of_boundary/test_output_boundary.py` |
| Boundary | audit trail, serialization, data-source seam, checkpoint safety | `tests/proof_of_boundary/test_pb_ret_c2_158.py` |
| Boundary | backbone invoke order | `tests/proof_of_boundary/test_pb_invoke_order.py` |
| Boundary | import isolation | `tests/proof_of_boundary/test_import_isolation.py` |
| Boundary | state safety | `tests/proof_of_boundary/test_state_safety.py` |
| Boundary | human-review interrupt propagation (skipped: the template declares no review interrupt) | `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py` |
| Integration | the real HTTP entry point, end to end | `tests/integration/test_invoke_end_to_end.py` |
| Integration | error-envelope containment | `tests/integration/test_error_envelope_containment.py` |
| Integration | entrypoint identity against the manifest | `tests/integration/test_manifest_identity_alignment.py` |

`tests/integration/asgi.py` is a small synchronous driver for the application
under test, not a test module.

## 2. The caller contract

| Case | Input | Expected |
|---|---|---|
| CC-01 | records as structured parameters | accepted; every record validated into its normalized form; `record_source` is `caller` |
| CC-02 | no records | accepted; `record_source` is `baseline` |
| CC-03 | records mandatory (`data_source: caller`) and none sent | refused, `input_context` named |
| CC-04 | empty, whitespace-only, non-string or non-JSON request | refused, field named |
| CC-05 | request over the character cap, more than 500 records in a list, more than 500 filter entries | refused |
| CC-06 | period malformed, in the future, older than 36 months, or carrying a trailing newline | refused |
| CC-07 | an identifier with a space, a trailing newline, a heading marker, over 32 characters, non-ASCII — on every rendered field | refused |
| CC-08 | `NaN`, `Infinity`, `-Infinity`, an overflowing literal, a boolean, a numeric string, a negative or over-range value — on every numeric field of every record type | refused, field named |
| CC-09 | an impossible calendar date, a non-anchored date | refused |
| CC-10 | an undeclared context key, an undeclared record field, a missing required field | refused, not dropped |
| CC-11 | a duplicate purchase order; a second delivery or invoice for one order; a line referencing no purchase order | refused |
| CC-12 | several returns for one order | accepted |
| CC-13 | `output_format` other than `markdown` | refused |
| CC-14 | the supplier filter declared on both channels | refused as ambiguous |
| CC-15 | chat-template control tokens (`<\|im_start\|>`, `<\|system\|>`, `[INST]`, `<<SYS>>`, `<system>`), instruction phrases, a directive spliced with markup or padded with zero-width characters, a hostile field name, a `\u`-escaped token | refused |
| CC-15a | a directive whose spaces are REPLACED by a zero-width character, for each character in the invisible class | refused; closing the run up would yield `ignoreall` and match nothing, so the separator normalization is screened as well as the joining one |
| CC-16 | ordinary procurement text containing "override", "disregard", "instructions", "you are now" | accepted |
| CC-17 | every credential shape the platform recognises and every local addition | refused; an inert-by-alphabet AWS key id included |
| CC-18 | any refusal | names the field, never repeats the value |

## 3. Runtime tuning

| Case | Expected |
|---|---|
| RT-01 | an absent block takes the shipped defaults |
| RT-02 | an unknown key, an out-of-range or non-finite tolerance, a boolean, weights that do not sum to 1.0, weights missing a KPI | refused at compile time by both graphs (`ConfigError`) |
| RT-03 | the outer graph seeds the validated block; the main slot forwards it; the inner graph seeds it |
| RT-04 | the shipped `config/config.yaml` parses to the defaults |

## 4. Domain steps

| Case | Target | Expected |
|---|---|---|
| DS-01 | ingestion | the sample dataset populates all four lists, per period; the supplier filter restricts it; an unknown filter yields empty lists without error |
| DS-02 | ingestion | caller records are published as given and win over the sample whatever the deployment says; the filter follows the purchase order for lines without a supplier |
| DS-03 | ingestion | a missing request is refused; a caller-only deployment refuses the baseline |
| DS-04 | discrepancy | the mixed fixture yields one shortfall (MEDIUM, `shortfall: 5`), one late delivery (HIGH, `days_late: 15`), one invoice mismatch (MEDIUM, `price_deviation_pct: 10.0`); no unit price appears in any detail line |
| DS-05 | discrepancy | clean records raise no flag; an early delivery is not late; a zero-quantity return raises no flag |
| DS-06 | discrepancy | the seeded tolerances absorb a delay and a deviation; a malformed seeded block falls back to the defaults |
| DS-07 | metrics | OTD 50.0, fill 100.0, OTIF 50.0 for the two-delivery fixture; composite 77.5 under the default weights; defect quantity read from the structured field; an invoice mismatch lowers invoice accuracy |
| DS-08 | metrics | rankings descend by score with ties broken on the identifier; the seeded weights change the composite |
| DS-09 | report | every section present; metadata populated; the data source stated; HIGH alerts rendered and capped; recommendations follow the KPIs; zero suppliers handled |
| DS-10 | every step | an earlier failure is carried forward, never overwritten with success |

## 5. The output boundary

| Case | Released content | Expected |
|---|---|---|
| OB-01 | every credential shape the platform detector recognises | refused by this gate first, with a reason label |
| OB-02 | a credential-assignment line, a `pk-` key, a short bearer value, a private-key header (shapes the platform does not recognise) | refused |
| OB-03 | a leak nested inside the metadata or a list | refused |
| OB-04 | a clean nested structure | released (the control that keeps OB-03 honest) |
| OB-05 | a violation | every output-bearing field is PRESENT in the returned update and cleared |
| OB-06 | a violation | the replacement notice is truthy and carries no detail |
| OB-07 | nothing rendered, or a rendered scorecard over 200,000 characters | refused with a truthy notice |
| OB-08 | a real scorecard, and structural tokens (identifiers, dates, `90d`, `STAR 2026`, `sk-1`, `Bearer`) | released byte-identical |
| OB-09 | the return shape of the previous gate on a leaking input | reproduced as leaking, so the fix is falsifiable |

## 6. End to end, through the HTTP entry point

| Case | Expected |
|---|---|
| E2E-01 | an authenticated request at the declared trust level succeeds with a non-empty scorecard and the backbone reaches the output gate |
| E2E-02 | the scorecard is computed from the caller's records (KPI rows pinned); a different request gives a different scorecard; the numbers move with the input |
| E2E-03 | no unit price appears in the response |
| E2E-04 | MEDIUM flags raise no alert; HIGH flags are rendered as alerts; clean records yield no flags and no action items; the baseline path is labelled |
| E2E-05 | the supplier filter is honoured on both channels; the structured channel is the route around the platform's personal-data masking (both directions proven) |
| E2E-06 | each declared runtime value changes the scorecard: late-delivery tolerance, price tolerance, composite weights, `data_source: caller` |
| E2E-07 | a missing or wrong caller credential is refused, indistinguishably; the runner credential is accepted; an unconfigured deployment refuses with 503 |
| E2E-08 | every invalid request is refused with 400 naming the field: period, format, filter, unknown keys, non-inert identifiers (trailing newline included), impossible dates, invalid quantities, duplicates, orphans, caps |
| E2E-09 | a credential-shaped record value is refused readably and never echoed; an ordinary identifier on the same field passes; a hostile field name is reported by position |
| E2E-10 | oversized structured parameters are refused with 413 |
| E2E-11 | `NaN`, `Infinity`, `-Infinity` and `1e400` sent as bare JSON tokens are refused on every numeric field |
| E2E-12 | injection payloads on either channel reach no scorecard |
| E2E-13 | a refused request carries no scorecard, no traceback and no source path; the notice names the field |
| E2E-14 | `deploy/invoke_payload.json` equals the suite's fixture and succeeds through the real app |

## 7. Error-envelope containment

The envelope resolves the released output as `formatted_output or result`, with
no status check, so a gate that raised — or that set an error status without
clearing — would still ship the un-gated scorecard inside the error envelope.

| Case | Expected |
|---|---|
| EC-01 | with a drifted scorecard on the DATA path, the envelope carries no leaked value, the status is an error and the output is the withheld notice |
| EC-02 | the gate node appears in `node_history`, proving the block happened there; no traceback and no source path reach the surface |
| EC-03 | with the gate's own scan disabled (the release-drift case), the platform raises inside the gate node, the clearing is discarded, and the envelope override alone keeps the scorecard out: the output is None and carries no leaked value |
| EC-04 | without the drift, the same request still returns its real scorecard and the gate node still runs |

The fault is injected on the data path, never on the gate: patching the gate
would test the patch rather than the agent.

## 8. Identity alignment

| Case | Expected |
|---|---|
| ID-01 | the manifest declares `namespace`, `name` and `industry` |
| ID-02 | the entrypoint provisions secrets under the manifest's `namespace` and `name`, both read from the files |
| ID-03 | `namespace` is `lower(industry)` |
| ID-04 | the manifest's dotted `class:` resolves to the class the adapter serves |

## 9. Release thresholds

- Every case above passes under the framework wheel the build pipeline installs.
- The output invariant holds for every representation probed, in both
  directions: leak forms refused, ordinary scorecard content released
  unchanged.
- No credential or model object is present in any state field at any stage.
