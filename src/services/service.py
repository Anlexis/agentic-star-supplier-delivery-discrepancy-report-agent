"""AgentCore Platform v1.0"""

# Service layer for RET-C2-158 — the single definition of what a caller may
# send, what the runtime configuration may declare, and what the output gate
# refuses to release.
#
# No business logic, no routing, no credentials. The HTTP adapter and the
# backbone nodes call the same helpers, so the two entry paths — a standalone
# process and the platform gateway calling invoke() directly — enforce one
# contract instead of two that can drift.
#
# Rules that hold for every caller value:
#
#   1. inert identifiers — every caller string that is rendered into the
#      scorecard (supplier ids, purchase-order ids, line items, return reasons,
#      the supplier filter) is restricted to a closed alphabet, anchored with
#      \A and \Z rather than ^ and $. In Python, `$` also matches immediately
#      BEFORE a trailing newline, so `^[A-Za-z0-9_-]{1,32}$` accepts
#      "SUP-001\n" — and these values are rendered into table rows and
#      headings, where a newline is precisely the character that lets a caller
#      string open a second line and read as report structure;
#
#   2. finite bounded numbers — every quantity and price is a real, finite
#      number inside an explicit range. NaN is the dangerous case rather than an
#      exotic one: it survives float(), and it compares False against every
#      bound, so an unchecked NaN quantity would silently defeat the shortfall
#      and defect comparisons the scorecard exists to make;
#
#   3. strict dates — YYYY-MM-DD, anchored, and a real calendar date, because a
#      date that fails to parse would otherwise silently drop the late-delivery
#      check for that order;
#
#   4. a closed schema — an undeclared record field or context key is REFUSED,
#      not dropped. Dropping would hand the caller a success with a silently
#      ignored value, and forwarding would put an unvetted value into state,
#      where the framework's own scan reads it out of the first node's result
#      and fails the request with a traceback nobody can act on;
#
#   5. instruction-override screening — caller text is refused when it carries
#      a chat-template control token or an explicit directive to discard the
#      agent's own instructions, screened raw AND with markup removed, keys
#      included;
#
#   6. credential screening as a UNION — the framework's own detector is the
#      floor and the local patterns are additions it does not carry. Narrower
#      than the framework is a bypass: the value clears the local gate, the
#      framework's own output scan then raises inside the node wrapper, and the
#      wrapper discards the whole delta, including the fields the local gate had
#      just cleared;
#
#   7. a refusal names the field, never the value.

from __future__ import annotations

import json
import math
import re
from datetime import date
from typing import Any, Dict, List, Mapping, Optional, Tuple

from framework.security.credential_detector import detect_credentials

# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

# The request string is a small JSON object: a period and an optional filter of
# at most 500 identifiers. Anything larger is bulk data on the wrong channel.
MAX_INPUT_CHARS = 32_768
# Serialized size of the structured parameters, checked at the HTTP door.
MAX_CONTEXT_BYTES = 262_144
MAX_SUPPLIER_FILTER = 500
MAX_RECORDS_PER_LIST = 500
MAX_CONTEXT_DEPTH = 6
# Reporting periods older than this are refused; the period must not be in the
# future either.
MAX_PERIOD_MONTHS_BACK = 36
QTY_MIN, QTY_MAX = 0, 1_000_000_000
PRICE_MIN, PRICE_MAX = 0.0, 1_000_000_000.0
# The rendered scorecard is bounded by the record caps; a report beyond this is
# not a scorecard and is withheld rather than released.
MAX_REPORT_CHARS = 200_000

# ---------------------------------------------------------------------------
# Inert alphabets — anchored with \A and \Z (see the module note, rule 1)
# ---------------------------------------------------------------------------

_INERT_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{1,32}\Z")
_SAFE_FIELD_NAME_RE = re.compile(r"\A[A-Za-z0-9_]{1,32}\Z")
_PERIOD_RE = re.compile(r"\A\d{4}-(?:0[1-9]|1[0-2])\Z")
_DATE_RE = re.compile(r"\A(\d{4})-(\d{2})-(\d{2})\Z")

RECORD_LISTS: Tuple[str, ...] = (
    "purchase_orders",
    "delivery_records",
    "defect_returns",
    "invoice_records",
)
ACCEPTED_CONTEXT_FIELDS: Tuple[str, ...] = RECORD_LISTS + ("supplier_filter",)

# Keys the platform itself puts into input_context, not the caller. The Marketplace
# runner invokes every agent as
#     agent.invoke(message, ctx=ctx, input_context={"conversation_history": history})
# (agenticstar-agentcore, shared/bootstrap/marketplace_app.py), whatever the user typed.
# Refusing it as an unknown field refused every chat request before the question was
# read. Discarded, not validated: nothing in this pipeline reads prior turns, and
# screening a transcript would let one earlier message refuse every later one. Discarding
# adds no exposure — the backbone's first node has already copied the raw input_context
# into state before this contract runs.
PLATFORM_RESERVED_KEYS = frozenset({"conversation_history"})
OUTPUT_FORMATS: Tuple[str, ...] = ("markdown",)
DATA_SOURCES: Tuple[str, ...] = ("mock", "caller")
KPI_WEIGHT_KEYS: Tuple[str, ...] = (
    "otd_pct",
    "fill_rate",
    "otif_pct",
    "defect_pct",
    "invoice_accuracy_pct",
)

# The shipped runtime defaults; config/config.yaml declares the same values.
DEFAULT_SETTINGS: Dict[str, Any] = {
    "data_source": "mock",
    "late_delivery_tolerance_days": 0,
    "price_tolerance_pct": 0.02,
    "kpi_weights": {
        "otd_pct": 0.25,
        "fill_rate": 0.25,
        "otif_pct": 0.20,
        "defect_pct": 0.15,
        "invoice_accuracy_pct": 0.15,
    },
}


def is_inert_token(value: Any) -> bool:
    """True when *value* is a short identifier over the closed inert alphabet."""
    return isinstance(value, str) and bool(_INERT_TOKEN_RE.match(value))


def safe_field_label(name: Any, position: int) -> str:
    """Return a field name safe to echo in a refusal, or a positional label.

    A hostile field name reaches the same log lines and refusal bodies a value
    does, so a name that is not already inert is never repeated back.
    """
    if isinstance(name, str) and _SAFE_FIELD_NAME_RE.match(name):
        return name
    return f"field #{position}"


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class ContractError(ValueError):
    """A caller value failed its check. Carries the field, never the value."""

    def __init__(self, field: str, reason: str) -> None:
        self.field = field
        self.reason = reason
        super().__init__(f"{field}: {reason}")


# ---------------------------------------------------------------------------
# Finite bounded numbers and strict dates
# ---------------------------------------------------------------------------


def finite_int_in_range(value: Any, lo: int, hi: int) -> Optional[int]:
    """Parse *value* as a real, finite whole number inside ``[lo, hi]``; else None.

    Rejects booleans (``isinstance(True, int)`` is true in Python), strings and
    other non-numeric types, NaN, the infinities and non-integral floats.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number != int(number):
        return None
    parsed = int(number)
    return parsed if lo <= parsed <= hi else None


def finite_float_in_range(value: Any, lo: float, hi: float) -> Optional[float]:
    """Parse *value* as a real, finite number inside ``[lo, hi]``; else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    return number if lo <= number <= hi else None


def parse_iso_date(value: Any) -> Optional[date]:
    """Parse a strict, anchored YYYY-MM-DD calendar date; else None."""
    if not isinstance(value, str):
        return None
    match = _DATE_RE.match(value)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def months_between(period: str, today: Optional[date] = None) -> int:
    """Whole months from the first day of *period* (YYYY-MM) to the current month."""
    now = today or date.today()
    year, month = int(period[:4]), int(period[5:7])
    return (now.year - year) * 12 + (now.month - month)


# ---------------------------------------------------------------------------
# Instruction-override screening
# ---------------------------------------------------------------------------

# Chat-template control tokens, screened as a CLASS rather than as a list of
# known strings: any `<|...|>` marker, the instruction brackets used by several
# instruction-tuned model families, the system-block markers and bare role tags.
# None of them carries meaning in a procurement request, so refusing the whole
# family costs nothing — and a phrase-only screen misses every one. The
# framework's own policy scores `<|im_start|>` and `[INST]` but not `<<SYS>>`.
_CONTROL_TOKEN_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("chat_control_token", re.compile(r"<\|[^|>]{0,64}\|>")),
    ("instruction_bracket", re.compile(r"\[/?INST]", re.IGNORECASE)),
    ("system_block_marker", re.compile(r"<</?SYS>>", re.IGNORECASE)),
    ("role_tag", re.compile(r"<\s*/?(?:system|assistant|user)\s*>", re.IGNORECASE)),
)

# Explicit instruction-override directives. Every pattern is anchored on a full
# verb+object phrase, because a bare verb is not enough: procurement vocabulary
# contains "override" and "disregard" in legitimate notes, and a screen that
# refuses real work is worse than no screen at all.
_DIRECTIVE_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (
        "instruction_override",
        re.compile(
            r"(?<![A-Za-z])(?:ignore|disregard|forget|override|bypass)\s+"
            r"(?:all\s+|any\s+|the\s+|your\s+|these\s+)*"
            r"(?:previous|prior|above|preceding|earlier|foregoing|system)?\s*"
            r"(?:instructions?|prompts?|directives?|constraints?)"
            r"(?![A-Za-z])",
            re.IGNORECASE,
        ),
    ),
    (
        "instruction_override_scoped",
        re.compile(
            r"(?<![A-Za-z])(?:ignore|disregard|forget|override|bypass)\s+"
            r"(?:(?:all|any|the|these)\s+)*"
            r"(?:(?:your|system|previous|prior|above|preceding|earlier|foregoing)\s+)+"
            r"(?:rules?|guidelines?|policies|policy)"
            r"(?![A-Za-z])",
            re.IGNORECASE,
        ),
    ),
    (
        "prompt_disclosure",
        re.compile(
            r"(?<![A-Za-z])(?:reveal|disclose|repeat|print|output|dump|show)\s+"
            r"(?:me\s+|us\s+)?(?:your|the)\s+(?:full\s+|entire\s+|original\s+)?"
            r"(?:system\s+prompt|system\s+message|initial\s+instructions)"
            r"(?![A-Za-z])",
            re.IGNORECASE,
        ),
    ),
    (
        "persona_override",
        re.compile(
            r"(?<![A-Za-z])you\s+are\s+(?:now|no\s+longer)\s+(?:an?|the)\s+"
            r"|(?<![A-Za-z])you\s+are\s+no\s+longer\s+"
            r"(?:bound|required|restricted|limited|allowed|obliged)(?![A-Za-z])",
            re.IGNORECASE,
        ),
    ),
    (
        "instruction_injection",
        re.compile(r"(?<![A-Za-z])new\s+(?:instructions?|rules?|system\s+prompt)\s*[:：]", re.IGNORECASE),
    ),
    (
        "instruction_override_ja",
        re.compile(
            r"(?:これまでの|以前の|上記の|すべての|全ての|前の)?"
            r"(?:指示|命令|プロンプト|ルール|制約)(?:を|は)?"
            r"(?:すべて|全て)?(?:無視|忘れ|破棄|上書き)"
        ),
    ),
    (
        "prompt_disclosure_ja",
        re.compile(r"(?:システムプロンプト|初期指示|システムメッセージ)(?:を|は)?(?:教え|出力|表示|開示|見せ)"),
    ),
)

# Markup and invisible characters are normalized away before the second and
# third screening passes, so a directive broken up with either is readable to
# the same patterns.
_MARKUP_TAG_RE = re.compile(r"<[^<>]{0,64}>")
_INVISIBLE_RE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff\u00ad]")


def strip_markup(text: str) -> str:
    """Remove inline markup and invisible characters, joining what they split."""
    return _MARKUP_TAG_RE.sub("", _INVISIBLE_RE.sub("", text))


def separate_markup(text: str) -> str:
    """Same removal, but treating each removed run as a word separator.

    Joining and separating are different normalizations and neither alone covers
    both directions. A directive spliced with a tag only reads as one after the
    tag is closed up; a directive whose spaces have been REPLACED by invisible
    characters only reads as one after those become the spaces they stand in
    for, because closing them up instead yields "ignoreall", which matches
    nothing. An invisible character can be used in place of a space as easily as
    in addition to one, so both normalizations are screened.
    """
    return _MARKUP_TAG_RE.sub(" ", _INVISIBLE_RE.sub(" ", text))


def screen_text(text: Any) -> Optional[str]:
    """Return the name of the first override pattern *text* carries, else None.

    Screens the value three ways — as received, so a control token is caught
    before a strip could remove it; with markup and invisible characters closed
    up, so a directive spliced across them is caught once re-assembled; and with
    the same runs turned into separators, so one standing in for a space is
    caught too. Stripping alone would be worse than doing nothing: it turns a
    detectable token attack into undetectable plain text and forwards the
    directive residue.
    """
    if not isinstance(text, str) or not text:
        return None
    for candidate in (text, strip_markup(text), separate_markup(text)):
        for name, pattern in _CONTROL_TOKEN_PATTERNS:
            if pattern.search(candidate):
                return name
        for name, pattern in _DIRECTIVE_PATTERNS:
            if pattern.search(candidate):
                return name
    return None


def screen_structure(value: Any, _depth: int = 0) -> Optional[str]:
    """Screen every string leaf AND every mapping key, depth-first.

    Applied to the PARSED request rather than to the raw body, so a directive
    hidden behind JSON ``\\u`` escapes — absent from the raw text, present in the
    parsed value — is still caught. Keys are screened because a hostile field
    name reaches the same audit and refusal paths a value does.
    """
    if _depth > MAX_CONTEXT_DEPTH:
        return "nesting_depth_exceeded"
    if isinstance(value, str):
        return screen_text(value)
    if isinstance(value, Mapping):
        for key, nested in value.items():
            found = screen_text(key) if isinstance(key, str) else None
            if found:
                return found
            found = screen_structure(nested, _depth + 1)
            if found:
                return found
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            found = screen_structure(item, _depth + 1)
            if found:
                return found
    return None


# ---------------------------------------------------------------------------
# Credential screening — the framework's detector plus what it does not carry
# ---------------------------------------------------------------------------

# Credential shapes the framework's own detector does not match. Measured
# against the installed wheel: detect_credentials() recognises stripe_key,
# openai_key (sk- plus 20+ characters), jwt, aws_key, bearer_token (16+
# characters) and conn_string, and returns nothing for `password=`, `token:`,
# a `pk-`/`ak-` prefixed key, a shorter `sk-` key, a bearer value carrying `~+/`
# or a private-key block. The shipped gate refused every one of those, and
# delegating wholesale to the framework would have made them newly releasable
# while looking like an upgrade. They are kept as ADDITIONS.
_EXTRA_CREDENTIAL_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    (
        "credential_assignment",
        re.compile(
            r"(?:password|passwd|secret|api[_-]?key|token|access[_-]?key|private[_-]?key)\s*[:=]\s*\S{6,}",
            re.IGNORECASE,
        ),
    ),
    ("api_key_prefixed", re.compile(r"(?<![A-Za-z0-9])(?:sk|pk|ak)-[A-Za-z0-9]{16,}", re.IGNORECASE)),
    ("bearer_token_wide", re.compile(r"Bearer\s+[A-Za-z0-9._~+/\-]{8,}")),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]{0,32}PRIVATE KEY-----")),
)


def detect_output_credentials(text: Any) -> Optional[str]:
    """Return the name of the first credential shape in *text*, else None.

    The framework's ``detect_credentials`` is the FLOOR, so this refusal set is a
    superset of the block set the framework enforces one layer later. Only the
    KIND is returned, never the matched text, so a refusal can be audited
    without re-emitting what it withheld.
    """
    if not isinstance(text, str) or not text:
        return None
    findings = detect_credentials(text)
    if findings:
        return str(findings[0]["type"])
    for name, pattern in _EXTRA_CREDENTIAL_PATTERNS:
        if pattern.search(text):
            return name
    return None


def scan_released_content(content: Any, _depth: int = 0) -> Optional[str]:
    """Scan a released structure depth-first; name the first violation or None.

    Walks nested mappings and sequences, scanning every leaf. A gate that looked
    only at top-level strings would report zero findings on a payload whose leak
    sits one level down.
    """
    if _depth > MAX_CONTEXT_DEPTH:
        return "nesting_depth_exceeded"
    if content is None:
        return None
    if isinstance(content, Mapping):
        for value in content.values():
            hit = scan_released_content(value, _depth + 1)
            if hit:
                return hit
        return None
    if isinstance(content, (list, tuple)):
        for item in content:
            hit = scan_released_content(item, _depth + 1)
            if hit:
                return hit
        return None
    return detect_output_credentials(str(content))


def _value_carries_credential(value: Any, _depth: int = 0) -> bool:
    if _depth > MAX_CONTEXT_DEPTH:
        return True
    if isinstance(value, str):
        return detect_output_credentials(value) is not None
    if isinstance(value, Mapping):
        return any(_value_carries_credential(item, _depth + 1) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_value_carries_credential(item, _depth + 1) for item in value)
    return False


def detect_context_credentials(context: Any) -> Optional[str]:
    """Return the label of the first context field carrying a credential shape.

    The framework's first node returns the caller's ``input_context`` verbatim
    into its own result, and the framework's output gate scans every value of
    every result — so a credential-shaped string anywhere in ``input_context``
    makes the FIRST node fail with a traceback the caller cannot act on. The
    request cannot succeed either way; screening here converts that opaque
    failure into an actionable refusal that names the field.

    Iterating top-level fields is exactly equivalent to scanning the whole
    mapping, because the framework defines its dict case as the union over
    ``.values()`` — which is what lets this refusal name a field without
    widening or narrowing the block set. Like the framework's own detector,
    this looks at values and never at keys.
    """
    if not isinstance(context, Mapping):
        return None
    for position, (key, value) in enumerate(context.items(), start=1):
        if _value_carries_credential(value):
            return safe_field_label(key, position)
    return None


# ---------------------------------------------------------------------------
# The caller contract
# ---------------------------------------------------------------------------

REASON_NOT_A_MAPPING = "must be an object"
REASON_UNKNOWN_FIELD = "is not a field this agent accepts"
REASON_NOT_INERT = "must be 1-32 characters of letters, digits, underscore or hyphen"
REASON_CREDENTIAL_SHAPE = "carries a credential-shaped value"

# Per-record schemas: field -> kind. "required" fields must be present.
_PO_FIELDS: Dict[str, str] = {
    "po_id": "token",
    "supplier_id": "token",
    "line_item": "token",
    "qty_ordered": "qty",
    "unit_price": "price",
    "planned_delivery_date": "date",
    "status": "token",
}
_PO_REQUIRED = ("po_id", "supplier_id", "qty_ordered", "planned_delivery_date")
_DELIVERY_FIELDS: Dict[str, str] = {
    "po_id": "token",
    "supplier_id": "token",
    "line_item": "token",
    "qty_received": "qty",
    "actual_delivery_date": "date",
}
_DELIVERY_REQUIRED = ("po_id", "qty_received", "actual_delivery_date")
_DEFECT_FIELDS: Dict[str, str] = {
    "po_id": "token",
    "supplier_id": "token",
    "qty_defective": "qty",
    "return_reason": "token",
}
_DEFECT_REQUIRED = ("po_id", "qty_defective")
_INVOICE_FIELDS: Dict[str, str] = {
    "po_id": "token",
    "supplier_id": "token",
    "line_item": "token",
    "invoiced_price": "price",
    "invoiced_qty": "qty",
}
_INVOICE_REQUIRED = ("po_id", "invoiced_price")

_RECORD_SCHEMAS: Dict[str, Tuple[Dict[str, str], Tuple[str, ...]]] = {
    "purchase_orders": (_PO_FIELDS, _PO_REQUIRED),
    "delivery_records": (_DELIVERY_FIELDS, _DELIVERY_REQUIRED),
    "defect_returns": (_DEFECT_FIELDS, _DEFECT_REQUIRED),
    "invoice_records": (_INVOICE_FIELDS, _INVOICE_REQUIRED),
}
# Lists in which one purchase order may appear at most once. A second delivery
# or invoice line for the same order would otherwise silently replace the
# first inside the matching index.
_ONE_PER_PO = ("purchase_orders", "delivery_records", "invoice_records")


def _field_value(kind: str, value: Any, where: str) -> Any:
    """Validate one record field by kind; raise ContractError naming *where*."""
    if kind == "token":
        if not is_inert_token(value):
            raise ContractError(where, REASON_NOT_INERT)
        if detect_output_credentials(value) is not None:
            raise ContractError(where, REASON_CREDENTIAL_SHAPE)
        return value
    if kind == "qty":
        parsed_qty = finite_int_in_range(value, QTY_MIN, QTY_MAX)
        if parsed_qty is None:
            raise ContractError(where, f"must be a whole number between {QTY_MIN} and {QTY_MAX}")
        return parsed_qty
    if kind == "price":
        parsed_price = finite_float_in_range(value, PRICE_MIN, PRICE_MAX)
        if parsed_price is None:
            raise ContractError(where, f"must be a finite number between {PRICE_MIN:g} and {PRICE_MAX:g}")
        return parsed_price
    parsed_date = parse_iso_date(value)
    if parsed_date is None:
        raise ContractError(where, "must be a calendar date in YYYY-MM-DD form")
    return parsed_date.isoformat()


def _validate_record(list_name: str, raw: Any, index: int) -> Dict[str, Any]:
    where = f"input_context.{list_name}[{index}]"
    if not isinstance(raw, Mapping):
        raise ContractError(where, REASON_NOT_A_MAPPING)
    fields, required = _RECORD_SCHEMAS[list_name]
    record: Dict[str, Any] = {}
    for position, (key, value) in enumerate(raw.items(), start=1):
        label = safe_field_label(key, position)
        if key not in fields:
            raise ContractError(f"{where}.{label}", REASON_UNKNOWN_FIELD)
        record[key] = _field_value(fields[key], value, f"{where}.{key}")
    for key in required:
        if key not in record:
            raise ContractError(f"{where}.{key}", "is required")
    return record


def validate_records(context: Mapping[str, Any]) -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Validate the four record lists carried in ``input_context``.

    Returns None when the context carries no record list at all (the pipeline
    then uses the configured baseline), otherwise a dict holding all four lists,
    absent ones empty. Every record is validated field by field against the
    closed schema; one purchase order may appear at most once per list, and a
    delivery, return or invoice must reference a purchase order in the request.
    """
    if not any(name in context for name in RECORD_LISTS):
        return None
    validated: Dict[str, List[Dict[str, Any]]] = {}
    for name in RECORD_LISTS:
        raw_list = context.get(name, [])
        where = f"input_context.{name}"
        if not isinstance(raw_list, (list, tuple)):
            raise ContractError(where, "must be a list of records")
        if len(raw_list) > MAX_RECORDS_PER_LIST:
            raise ContractError(where, f"must hold at most {MAX_RECORDS_PER_LIST} records")
        validated[name] = [_validate_record(name, raw, index) for index, raw in enumerate(raw_list)]

    known_orders = set()
    for index, order in enumerate(validated["purchase_orders"]):
        if order["po_id"] in known_orders:
            raise ContractError(f"input_context.purchase_orders[{index}].po_id", "is declared more than once")
        known_orders.add(order["po_id"])
    for name in ("delivery_records", "defect_returns", "invoice_records"):
        seen = set()
        for index, record in enumerate(validated[name]):
            if record["po_id"] not in known_orders:
                raise ContractError(f"input_context.{name}[{index}].po_id", "does not match any purchase order")
            if name in _ONE_PER_PO:
                if record["po_id"] in seen:
                    raise ContractError(f"input_context.{name}[{index}].po_id", "is declared more than once")
                seen.add(record["po_id"])
    return validated


def validate_period(value: Any, today: Optional[date] = None) -> str:
    """Accept a reporting period YYYY-MM that is not in the future and not older than the window."""
    if not isinstance(value, str) or not _PERIOD_RE.match(value):
        raise ContractError("period", "must be a reporting month in YYYY-MM form")
    now = today or date.today()
    if value > now.strftime("%Y-%m"):
        raise ContractError("period", "must not be in the future")
    if months_between(value, now) > MAX_PERIOD_MONTHS_BACK:
        raise ContractError("period", f"must be within the last {MAX_PERIOD_MONTHS_BACK} months")
    return value


def validate_supplier_filter(value: Any, where: str) -> List[str]:
    """Accept a list of at most 500 inert supplier identifiers."""
    if not isinstance(value, (list, tuple)):
        raise ContractError(where, "must be a list of supplier identifiers")
    if len(value) > MAX_SUPPLIER_FILTER:
        raise ContractError(where, f"must hold at most {MAX_SUPPLIER_FILTER} identifiers")
    accepted: List[str] = []
    for index, item in enumerate(value):
        accepted.append(_field_value("token", item, f"{where}[{index}]"))
    return accepted


def validate_request(
    user_input: Any,
    input_context: Any = None,
    *,
    require_records: bool = False,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Validate a whole request and return the contract the pipeline runs on.

    ``user_input`` is the request object — a JSON string or an already-parsed
    mapping — carrying ``period`` (required), ``supplier_filter`` and
    ``output_format``. ``input_context`` carries the structured records and may
    carry ``supplier_filter`` instead of the request object (the platform
    rewrites personal-data shapes out of the request string at every node
    boundary, so an identifier that resembles a phone or resident number arrives
    intact only on the structured channel). Declaring the filter on both
    channels is refused as ambiguous.

    Raises ContractError naming the offending field. The caller's raw values
    never appear in the message and never leave this function except in their
    validated, inert form.
    """
    if isinstance(user_input, str):
        if not user_input.strip():
            raise ContractError("input", "is empty")
        if len(user_input) > MAX_INPUT_CHARS:
            raise ContractError("input", f"must be at most {MAX_INPUT_CHARS} characters")
        if detect_output_credentials(user_input) is not None:
            raise ContractError("input", REASON_CREDENTIAL_SHAPE)
        hit = screen_text(user_input)
        if hit is not None:
            raise ContractError("input", f"contains a disallowed instruction pattern ({hit})")
        try:
            payload = json.loads(user_input)
        except (json.JSONDecodeError, ValueError):
            raise ContractError("input", "must be a JSON object with a period field") from None
    elif isinstance(user_input, Mapping):
        payload = dict(user_input)
    else:
        raise ContractError("input", "must be a JSON object with a period field")
    if not isinstance(payload, Mapping):
        raise ContractError("input", "must be a JSON object with a period field")
    hit = screen_structure(payload)
    if hit is not None:
        raise ContractError("input", f"contains a disallowed instruction pattern ({hit})")

    context: Dict[str, Any]
    if input_context in (None, {}):
        context = {}
    elif isinstance(input_context, Mapping):
        context = dict(input_context)
    else:
        raise ContractError("input_context", REASON_NOT_A_MAPPING)
    for reserved in PLATFORM_RESERVED_KEYS:
        context.pop(reserved, None)
    hit = screen_structure(context)
    if hit is not None:
        raise ContractError("input_context", f"contains a disallowed instruction pattern ({hit})")
    offending = detect_context_credentials(context)
    if offending is not None:
        raise ContractError(f"input_context.{offending}", REASON_CREDENTIAL_SHAPE)
    for position, key in enumerate(context, start=1):
        if key not in ACCEPTED_CONTEXT_FIELDS:
            raise ContractError(f"input_context.{safe_field_label(key, position)}", REASON_UNKNOWN_FIELD)

    for position, key in enumerate(payload, start=1):
        if key not in ("period", "supplier_filter", "output_format"):
            raise ContractError(f"input.{safe_field_label(key, position)}", REASON_UNKNOWN_FIELD)
    period = validate_period(payload.get("period"), today)

    output_format = payload.get("output_format", "markdown")
    if output_format not in OUTPUT_FORMATS:
        raise ContractError("output_format", f"must be one of {', '.join(OUTPUT_FORMATS)}")

    supplier_filter: Optional[List[str]] = None
    if "supplier_filter" in payload and "supplier_filter" in context:
        raise ContractError("supplier_filter", "is declared on both input and input_context")
    if payload.get("supplier_filter") is not None:
        supplier_filter = validate_supplier_filter(payload["supplier_filter"], "supplier_filter")
    elif context.get("supplier_filter") is not None:
        supplier_filter = validate_supplier_filter(context["supplier_filter"], "input_context.supplier_filter")

    records = validate_records(context)
    if records is None and require_records:
        raise ContractError("input_context", "must carry the purchase-order records for this deployment")

    return {
        "period": period,
        "supplier_filter": supplier_filter,
        "output_format": output_format,
        "records": records,
        "record_source": "caller" if records is not None else "baseline",
    }


# ---------------------------------------------------------------------------
# Runtime settings — the ret_c2_158 block of config/config.yaml
# ---------------------------------------------------------------------------


def parse_domain_settings(raw: Any) -> Dict[str, Any]:
    """Validate the runtime tuning block into its normalized form.

    Raises ValueError naming the key. Absent keys take the shipped defaults; a
    present but malformed value is refused rather than coerced, so a typo in
    the operator's configuration surfaces at compile time instead of changing
    the scorecard silently.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise ValueError("ret_c2_158 must be a mapping")
    for key in raw:
        if key not in DEFAULT_SETTINGS:
            raise ValueError(f"ret_c2_158.{key} is not a recognised setting")

    data_source = raw.get("data_source", DEFAULT_SETTINGS["data_source"])
    if data_source not in DATA_SOURCES:
        raise ValueError(f"ret_c2_158.data_source must be one of {', '.join(DATA_SOURCES)}")

    tolerance_days = finite_int_in_range(
        raw.get("late_delivery_tolerance_days", DEFAULT_SETTINGS["late_delivery_tolerance_days"]), 0, 365
    )
    if tolerance_days is None:
        raise ValueError("ret_c2_158.late_delivery_tolerance_days must be a whole number of days between 0 and 365")

    price_tolerance = finite_float_in_range(
        raw.get("price_tolerance_pct", DEFAULT_SETTINGS["price_tolerance_pct"]), 0.0, 1.0
    )
    if price_tolerance is None:
        raise ValueError("ret_c2_158.price_tolerance_pct must be a finite fraction between 0 and 1")

    raw_weights = raw.get("kpi_weights", DEFAULT_SETTINGS["kpi_weights"])
    if not isinstance(raw_weights, Mapping):
        raise ValueError("ret_c2_158.kpi_weights must be a mapping")
    if set(raw_weights) != set(KPI_WEIGHT_KEYS):
        raise ValueError(f"ret_c2_158.kpi_weights must declare exactly {', '.join(KPI_WEIGHT_KEYS)}")
    weights: Dict[str, float] = {}
    for key in KPI_WEIGHT_KEYS:
        weight = finite_float_in_range(raw_weights[key], 0.0, 1.0)
        if weight is None:
            raise ValueError(f"ret_c2_158.kpi_weights.{key} must be a finite number between 0 and 1")
        weights[key] = weight
    if abs(sum(weights.values()) - 1.0) > 1e-6:
        raise ValueError("ret_c2_158.kpi_weights must sum to 1.0")

    return {
        "data_source": data_source,
        "late_delivery_tolerance_days": tolerance_days,
        "price_tolerance_pct": price_tolerance,
        "kpi_weights": weights,
    }


def settings_from_state(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Read the seeded runtime settings, falling back to the shipped defaults."""
    seeded = state.get("domain_settings")
    if isinstance(seeded, Mapping):
        try:
            return parse_domain_settings(seeded)
        except ValueError:
            return parse_domain_settings(None)
    return parse_domain_settings(None)
