"""AgentCore Platform v1.0"""

# Standalone HTTP entry point for the agent.
# Entry points are adapters only — no business logic here.
# For platform-level routing, AgentGateway calls agent.invoke() directly and
# this module is never loaded, which is exactly why the caller contract is
# enforced in the pre_process node as well as here. The two are not
# duplicates: they cover two different entry paths, and a rule owned only by
# this file would be absent on the other.
#
# Four things this adapter owns:
#
#   1. Caller authentication. The agent's trust boundary admits only a
#      VERIFIED_EXTERNAL caller, and an unauthenticated caller is ANONYMOUS.
#      Nothing else sets request.state.trust_level in a standalone deployment,
#      so without this every request would be refused inside the graph and
#      answered HTTP 200 with an error body and a null output — a deployment
#      that reports healthy and serves nothing. A deployment with no token
#      configured refuses at the door, with 503 and a reason.
#
#   2. The shape of the structured parameters, before invoke(). The framework's
#      first node returns the caller's context verbatim into its own result,
#      and the framework's output gate scans every value of every result — so
#      a credential-shaped string anywhere in input_context fails node one with
#      a traceback the caller cannot act on. The request cannot succeed either
#      way; this adapter refuses it with the field named.
#
#   3. Bounding what enters the graph — the request string, the session
#      identifier and the serialized size of the structured parameters — and
#      running the same caller contract the pre_process node enforces, so an
#      invalid request receives a 400 naming the field rather than a 200 with
#      an error body.
#
#   4. The identity the agent's secrets are provisioned under, which matches
#      config/agent.yaml (namespace = lower(industry) = "ret"). The secret
#      provider is scoped by that identity, so a namespace that disagrees with
#      the manifest silently splits one agent's secrets across two stores.
#      tests/integration/test_manifest_identity_alignment.py reads both sides
#      rather than restating them.
#
# 400 is used rather than 422: pydantic owns 422 and returns a list of error
# objects there, so reusing it would make client handling ambiguous.

import json
import os
import secrets
from pathlib import Path
from typing import Any, Dict, cast
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.context import bound_secrets
from framework.utils.config_loader import load_agent_config
from pydantic import BaseModel, Field
from shared.secrets import factory as secrets_factory
from shared.utils.audit_logger import emit_trace_event

from src.graph.graph import RetC2158Agent
from src.services.service import (
    MAX_CONTEXT_BYTES,
    MAX_INPUT_CHARS,
    ContractError,
    detect_context_credentials,
    detect_output_credentials,
    is_inert_token,
    validate_request,
)

app = FastAPI(title="Agent")

# Same config_dir / "config.yaml" convention as AgentRegistry._compile_and_cache():
# an absent config.yaml is tolerated, matching the registry's own guard. Without
# this the standalone adapter would run with config={} and the declared runtime
# values — max_retry, the ret_c2_158 tuning — would never reach the graph on
# this path.
_REPO_ROOT = Path(__file__).resolve().parents[2]

agent = RetC2158Agent(config=load_agent_config(_REPO_ROOT))
agent.compile()
# Namespace and agent name match config/agent.yaml.
agent.provision_secrets(secrets_factory(namespace="ret", agent_name="SupplierDeliveryPerformanceReportAgent"))


class InvokeRequest(BaseModel):
    input: str
    session_id: str = ""
    # Structured invocation parameters: the purchase-order, delivery, return
    # and invoice records (and optionally the supplier filter) — validated field
    # by field against the closed contract in src/services/service.py.
    input_context: Dict[str, Any] = Field(default_factory=dict)


def _authenticate(request: Request) -> TrustLevel:
    """Resolve the caller's trust level, or refuse.

    A deployment may present either the ordinary external bearer or the
    separate runner credential; both authenticate the same VERIFIED_EXTERNAL
    caller this agent admits. Accepting both matters because the sign-off
    harness presents whichever one the manifest's trust level implies, and an
    adapter that reads only one of them fails the deployment check with nothing
    pointing at the credential. Middleware-established trust is never changed.
    """
    trust = getattr(request.state, "trust_level", TrustLevel.ANONYMOUS)
    if trust is not TrustLevel.ANONYMOUS:
        return cast(TrustLevel, trust)

    configured = [
        token for token in (os.environ.get("INVOKE_AUTH_TOKEN"), os.environ.get("STG_INTERNAL_RUNNER_TOKEN")) if token
    ]
    if not configured:
        # Nothing can authenticate a caller, so nothing this endpoint returns
        # could be an answer. Say so once, at the door, instead of refusing
        # four nodes later with a 200 and a null output.
        raise HTTPException(status_code=503, detail="Caller authentication is not configured on this deployment.")

    supplied = request.headers.get("authorization", "").encode()
    # Compare bytes: compare_digest raises TypeError on non-ASCII str input
    # (headers decode as latin-1), which would surface as a 500 rather than the
    # generic 401 below.
    if not any(secrets.compare_digest(supplied, f"Bearer {token}".encode()) for token in configured):
        # Generic body on purpose — it must not reveal whether the token was
        # absent, malformed or simply wrong.
        raise HTTPException(status_code=401, detail="Token is invalid or expired.")
    return TrustLevel.VERIFIED_EXTERNAL


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request) -> Dict[str, Any]:
    trust = _authenticate(request)

    if not req.input.strip():
        raise HTTPException(status_code=400, detail="Field 'input' is empty.")
    if len(req.input) > MAX_INPUT_CHARS:
        raise HTTPException(status_code=400, detail=f"Field 'input' exceeds the {MAX_INPUT_CHARS}-character limit.")
    if req.session_id and not is_inert_token(req.session_id):
        raise HTTPException(status_code=400, detail="Field 'session_id' must be 1-32 characters of [A-Za-z0-9_-].")
    for field, value in (("input", req.input), ("session_id", req.session_id)):
        if detect_output_credentials(value):
            raise HTTPException(status_code=400, detail=f"Field '{field}' carries a credential-shaped value.")

    if len(json.dumps(req.input_context, default=str)) > MAX_CONTEXT_BYTES:
        raise HTTPException(status_code=413, detail="Field 'input_context' exceeds the maximum allowed size.")
    offending = detect_context_credentials(req.input_context)
    if offending is not None:
        emit_trace_event("input_context_credential_refused", {"field": offending}, {"session_id": req.session_id})
        raise HTTPException(
            status_code=400,
            detail=(
                f"Field 'input_context.{offending}' carries a credential-shaped value. Remove API keys, "
                "tokens and connection strings from the records and retry."
            ),
        )

    # The same contract the pre_process node enforces, so an invalid request is
    # answered with a 400 naming the field here and refused identically on the
    # platform's direct invoke() path.
    try:
        validate_request(
            req.input,
            req.input_context,
            require_records=agent.domain_settings["data_source"] == "caller",
        )
    except ContractError as refusal:
        raise HTTPException(status_code=400, detail=f"Field '{refusal.field}' {refusal.reason}.") from None

    with bound_secrets(agent._secrets_provider):
        ctx = InvocationContext(
            session_id=req.session_id or str(uuid4()),
            caller_trust_level=trust,
            caller_id=getattr(request.state, "caller_id", ""),
        )
        # The framework wheel ships no type information, so invoke() is Any.
        return cast(Dict[str, Any], agent.invoke(req.input, ctx=ctx, input_context=dict(req.input_context)))


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "agent": "SupplierDeliveryPerformanceReportAgent"}
