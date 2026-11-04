"""app/muse_verify: the live Muse Spark verify step that app/core calls.

Contract (app/core/verifier.py docstring is canonical):
    get_verifier(**kwargs) -> MuseVerifier with
        .mode  : "live" | "mock" -- taken from the transport, so a mock can never be labeled live
        .model : str                       Meta Model API model id (owned by the API layer; never hard-coded here)
        .decompose_claim(req)   M1  req = {"case_id","claim_text","source_type"}
                                    -> {"assertions": [{"assertion_id","text","kind","checkable"}]}
        .classify_evidence(req) M2  req = {"case_id","assertion":{...},"receipt":{kind,ref,note,...}}
                                    -> {"relation": supports|contradicts|insufficient, "rationale"<=300, "quoted_span"}
        .explain_verdict(req)   M3  req = {"case_id","state","receipts":[...]} -> {"summary": <=2 sentences}
                                    UI/video only. NEVER an input to the gate.

Transport (the only network surface) is NOT built here. The design assigns base URL, auth, model id and the
credential env-var name to the API layer (app/api). This module accepts a transport from, in order:
    1. get_verifier(transport=<callable>, model=<str>)            explicit injection (tests, harness)
    2. app.api.get_transport() -> (callable, model_id)             the API layer
and raises VerifierError when neither is available. It never opens a socket at construction.

Transport protocol:  transport(call_name: str, system: str, user: str, schema: dict) -> str (raw model text)
  - temperature 0 / JSON-only is the transport's job (it owns the request format).
  - Transport failures raise NonRetryableTransportError and are never retried; malformed model responses raise
    RetryableResponseError and receive at most the core's single bounded schema retry.

No retry here: app/core/engine.py owns the one bounded schema retry; transport errors never reach that retry path.
"""
import importlib
import json
import re

from app.core.contracts import ASSERTION_KINDS, RELATIONS
from app.core.verifier import (NonRetryableTransportError, RetryableResponseError, VerifierError,
                               validate_m1, validate_m2)

__all__ = ["get_verifier", "MuseVerifier", "VerifierError", "NonRetryableTransportError",
           "RetryableResponseError", "M1_SCHEMA", "M2_SCHEMA", "M3_SCHEMA"]

# ---- output schemas (JSON-only; mirrored by validate_m1 / validate_m2 in app/core) ----------------
M1_SCHEMA = {
    "type": "object", "required": ["assertions"],
    "properties": {"assertions": {"type": "array", "items": {
        "type": "object", "required": ["assertion_id", "text", "kind", "checkable"],
        "properties": {"assertion_id": {"type": "string"}, "text": {"type": "string"},
                       "kind": {"enum": list(ASSERTION_KINDS)}, "checkable": {"type": "boolean"}}}}},
}
M2_SCHEMA = {
    "type": "object", "required": ["relation", "rationale", "quoted_span"],
    "properties": {"relation": {"enum": list(RELATIONS)}, "rationale": {"type": "string", "maxLength": 300},
                   "quoted_span": {"type": "string"}},
}
M3_SCHEMA = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}

# ---- prompts ---------------------------------------------------------------------------------------
_SYS_COMMON = (
    "You are the reading step of a self-verifying research agent. You output ONE JSON object and nothing "
    "else: no prose, no markdown fences. You never guess facts that are not in the input. "
)

M1_SYSTEM = _SYS_COMMON + (
    "TASK M1 decompose_claim: split the claim into the smallest independently checkable assertions. "
    f"Each assertion has: assertion_id ('A1','A2',... in order), text (verbatim or minimal paraphrase), "
    f"kind (one of {list(ASSERTION_KINDS)}), checkable (true only if a document, export, invoice, log or "
    "measurement could confirm or contradict it; opinions and vague praise are false). "
    'Output: {"assertions": [...]}'
)

M2_SYSTEM = _SYS_COMMON + (
    "TASK M2 classify_evidence: decide how ONE receipt bears on ONE assertion. relation is exactly one of "
    "'supports' (the receipt itself shows the assertion is true), 'contradicts' (the receipt itself shows it is "
    "false), 'insufficient' (anything else: missing, indirect, estimated, unverified, out of scope or out of "
    "date). Absence of evidence is 'insufficient', NEVER 'contradicts'. rationale <= 300 characters. "
    "quoted_span = the exact words from the receipt you relied on ('' if none). "
    'Output: {"relation": ..., "rationale": ..., "quoted_span": ...}'
)

M3_SYSTEM = _SYS_COMMON + (
    "TASK M3 explain_verdict: given a final verdict state already decided by a deterministic gate and the "
    "receipts it cited, write at most 2 plain sentences explaining why. Do not change or question the state. "
    'Output: {"summary": ...}'
)

_RECEIPT_KEYS = ("receipt_id", "kind", "ref", "retrieved_at", "note")  # what the model is allowed to read


def _receipt_view(r):
    return {k: r[k] for k in _RECEIPT_KEYS if k in r}


def m1_user(req):
    return json.dumps({"claim_text": req["claim_text"], "source_type": req.get("source_type")}, ensure_ascii=False)


def m2_user(req):
    a = req["assertion"]
    return json.dumps({"assertion": {k: a.get(k) for k in ("assertion_id", "text", "kind")},
                       "receipt": _receipt_view(req["receipt"])}, ensure_ascii=False)


def m3_user(req):
    return json.dumps({"state": req["state"], "receipts": [_receipt_view(r) for r in req.get("receipts", [])]},
                      ensure_ascii=False)


# ---- parsing ---------------------------------------------------------------------------------------
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


def parse_json_object(text, call_name):
    if not isinstance(text, str):
        raise RetryableResponseError(f"{call_name}: transport response was {type(text).__name__}, expected text")
    m = _FENCE.match(text)
    body = m.group(1) if m else text.strip()
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RetryableResponseError(f"{call_name}: model output is not JSON ({exc.msg} at {exc.pos})")
    if not isinstance(obj, dict):
        raise RetryableResponseError(f"{call_name}: model output is JSON {type(obj).__name__}, expected object")
    return obj


def validate_m3(resp):
    s = resp.get("summary") if isinstance(resp, dict) else None
    if not isinstance(s, str) or not s.strip():
        return "M3: summary must be a non-empty string"
    if len(re.findall(r"[.!?](?:\s|$)", s.strip())) > 2:
        return "M3: summary must be at most 2 sentences"
    return None


# ---- adapter ---------------------------------------------------------------------------------------
class MuseVerifier:
    mode = "live"

    def __init__(self, transport, model):
        if not callable(transport):
            raise VerifierError("muse_verify: transport is not callable")
        if not isinstance(model, str) or not model.strip():
            raise VerifierError("muse_verify: model id missing")
        self._t = transport
        self.model = model
        # The mode label follows the TRANSPORT, never this class. app/core/run.py writes it into every
        # A live label records routing intent only; run_summary separately checks for an HTTP-200 response.
        self.mode = getattr(transport, "mode", "live")

    @property
    def successful_http_responses(self):
        return int(getattr(self._t, "successful_http_responses", 0))

    @property
    def request_attempts(self):
        return int(getattr(self._t, "calls", 0))

    def _ask(self, name, system, user, schema, validator):
        try:
            raw = self._t(name, system, user, schema)
        except RetryableResponseError:
            raise
        except NonRetryableTransportError:
            raise
        except Exception as exc:
            raise NonRetryableTransportError(f"{name}: transport failure ({type(exc).__name__})") from exc
        resp = parse_json_object(raw, name)
        err = validator(resp)
        if err:
            raise RetryableResponseError(err)
        return resp

    def decompose_claim(self, req):
        return self._ask("M1", M1_SYSTEM, m1_user(req), M1_SCHEMA, validate_m1)

    def classify_evidence(self, req):
        resp = self._ask("M2", M2_SYSTEM, m2_user(req), M2_SCHEMA, validate_m2)
        resp.setdefault("quoted_span", "")
        return resp

    def explain_verdict(self, req):
        return self._ask("M3", M3_SYSTEM, m3_user(req), M3_SCHEMA, validate_m3)


def _transport_from_api():
    try:
        api = importlib.import_module("app.api")
    except ImportError as exc:
        raise VerifierError(
            "muse_verify is present but no live transport is configured: app/api (the API layer) "
            f"is not importable ({exc}). Base URL, auth, model id and key env-var are UNVERIFIED "
            ". Pass get_verifier(transport=..., model=...) or land app/api.get_transport()."
        )
    if not hasattr(api, "get_transport"):
        raise VerifierError("app.api has no get_transport() -> (callable, model_id); see app/muse_verify docstring")
    out = api.get_transport()
    if not (isinstance(out, tuple) and len(out) == 2):
        raise VerifierError("app.api.get_transport() must return (transport_callable, model_id)")
    return out


def get_verifier(transport=None, model=None, **_kwargs):
    """Build the live verifier. No network call happens here."""
    if transport is None:
        transport, api_model = _transport_from_api()
        model = model or api_model
    return MuseVerifier(transport, model)
