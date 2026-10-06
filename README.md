# Supplier Delivery Discrepancy Report Agent

AI agent for reporting supplier delivery performance and order discrepancies, built with Agentic Star.

> **Category**: Cat 2 (a domain-specific pipeline for one job-to-be-done)
> **Industry**: Retail
> **Template ID**: RET-C2-158

## Overview

Retail procurement teams review supplier performance every month, and the
review is usually assembled by hand: purchase orders from one system, goods
receipts from another, returns and invoices from a third, matched line by line
in a spreadsheet.

This agent does that assembly. Given a reporting month and the purchase-order,
delivery, return and invoice records for it, the agent matches every purchase
order against its delivery and invoice, flags the discrepancies — quantity
shortfalls, late deliveries, invoice price deviations, defect returns — computes
the standard supplier KPIs (on-time delivery, fill rate, on-time-in-full,
defect rate, invoice accuracy) and a weighted composite score, and returns a
ranked **supplier performance scorecard** in Markdown with the high-severity
alerts and the follow-up actions the numbers call for.

The scorecard is computed deterministically from the records it is given; no
language model is involved. Every value a caller sends is validated against a
closed contract before it is used, and nothing credential-shaped is released.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Without it, start-up fails immediately (see *Behaviour without the platform* below). Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Installed from PyPI as a dependency. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run **only** on AGENTIC STAR. There is no fallback or degraded
mode. If the platform is unreachable or the SDK version does not match, the agent fails at graph
compile / start-up preflight rather than starting in a partially working state. This is
intentional — a half-running agent is worse than one that refuses to start.

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection. Running the agent itself does not.

## Sending a request

The request object names the reporting month; the records belong in
`input_context`.

```json
{
  "input": "{\"period\": \"2026-01\"}",
  "input_context": {
    "purchase_orders": [
      {"po_id": "PO-2026-01-0001", "supplier_id": "SUP-ALPHA", "line_item": "WIDGET-A",
       "qty_ordered": 100, "unit_price": 5.0, "planned_delivery_date": "2026-01-15"}
    ],
    "delivery_records": [
      {"po_id": "PO-2026-01-0001", "qty_received": 95, "actual_delivery_date": "2026-01-15"}
    ],
    "defect_returns": [
      {"po_id": "PO-2026-01-0001", "qty_defective": 3, "return_reason": "cosmetic_defect"}
    ],
    "invoice_records": [
      {"po_id": "PO-2026-01-0001", "invoiced_price": 5.0, "invoiced_qty": 95}
    ]
  }
}
```

Identifiers are restricted to `[A-Za-z0-9_-]` (1–32 characters), quantities and
prices must be finite numbers in range, and dates must be real calendar dates in
`YYYY-MM-DD` form. An undeclared field is refused rather than ignored. A refused
request names the field that failed and never repeats the value. When no records
are sent, the agent renders the scorecard from a built-in sample dataset and says
so in the report. `docs/02_design.md` carries the full contract.

## Project Structure

```
src/          agent implementation (nodes, graphs, services, schemas)
tests/        unit, boundary and integration tests
config/       agent manifest and runtime parameters
deploy/       local deployment recipe and a smoke payload
docs/         design and operational documentation
```

`docs/` holds the design (`02_design.md`) and the test specification
(`03_test_spec.md`).

## Customising

1. Adjust `config/config.yaml` for your own policies — the late-delivery
   tolerance, the invoice price tolerance and the composite-score weights all
   change the scorecard.
2. Connect your own records: send them with each request, or replace the
   sample dataset in `src/nodes/data_ingestion_node.py` with a loader for your
   procurement system.
3. Review the discrepancy rules and KPI definitions under `src/nodes/` for
   domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
