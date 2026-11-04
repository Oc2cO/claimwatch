"""Muse Spark verify step: the adapter contract core calls, plus mock and replay adapters.

Contract (what app/muse_verify must expose for live mode):
    module app.muse_verify  ->  get_verifier(**kwargs) -> object with
        .model : str                                   e.g. the Meta Model API model id (provided by the API integration)
        .mode  : "live"
        .decompose_claim(req: dict) -> dict            M1  req = {"case_id","claim_text","source_type"}
              returns {"assertions": [{"assertion_id","text","kind","checkable"}]}
        .classify_evidence(req: dict) -> dict          M2  req = {"case_id","assertion":{...},"receipt":{kind,ref,note,...}}
              returns {"relation": supports|contradicts|insufficient, "rationale": str<=300, "quoted_span": str}
    Raise typed VerifierError subclasses for transport/schema failure; core records it, never passes it silently.

Core validates every response (validate_m1 / validate_m2). A model output alone never changes state (I4):
the gate in gate.py decides.

Adapters shipped here:
    MockVerifier   - deterministic FIXTURE ORACLE. M1 returns the fixture's pre-extracted assertions and
                     M2 returns the fixture's recorded relation. It is NOT Muse Spark and makes no network call.
                     It exists so the pipeline, gate, ledger and alerts are testable before credits exist.
    ReplayVerifier - replays responses recorded in a previous run's calls.jsonl, keyed by prompt_sha256.
"""
import json

from . import contracts as C
from .ledger import canonical, sha256_hex


class VerifierError(Exception):
    pass


class RetryableResponseError(VerifierError):
    """A completed adapter response failed parsing/schema checks; engine may use its one bounded retry."""


class NonRetryableTransportError(VerifierError):
    """A transport or exhausted-budget failure that must not trigger another paid request."""


def prompt_hash(name, req):
    return sha256_hex(canonical({"call": name, "req": req}))


def validate_m1(resp):
    if not isinstance(resp, dict) or not isinstance(resp.get("assertions"), list):
        return "M1: response must be {assertions: [...]}"
    seen = set()
    for i, a in enumerate(resp["assertions"]):
        if not isinstance(a, dict):
            return f"M1: assertions[{i}] not an object"
        for k in ("assertion_id", "text", "kind", "checkable"):
            if k not in a:
                return f"M1: assertions[{i}].{k} missing"
        if a["kind"] not in C.ASSERTION_KINDS:
            return f"M1: assertions[{i}].kind {a['kind']!r} not in enum"
        if not isinstance(a["checkable"], bool):
            return f"M1: assertions[{i}].checkable not bool"
        if a["assertion_id"] in seen:
            return f"M1: duplicate assertion_id {a['assertion_id']}"
        seen.add(a["assertion_id"])
    return None


def validate_m2(resp):
    if not isinstance(resp, dict):
        return "M2: response not an object"
    if resp.get("relation") not in C.RELATIONS:
        return f"M2: relation {resp.get('relation')!r} not in {C.RELATIONS}"
    if not isinstance(resp.get("rationale", ""), str) or len(resp.get("rationale", "")) > 300:
        return "M2: rationale must be a string <= 300 chars"
    return None


class MockVerifier:
    """Fixture oracle. Built by the HARNESS from full records; the engine never sees the records it holds."""

    mode = "mock"
    model = "mock-fixture-oracle"

    def __init__(self, full_records):
        self._claims = {r["case_id"]: r["claims"] for r in full_records}
        self._rel = {(r["case_id"], x["receipt_id"]): (x["relation"], x["note"]) for r in full_records for x in r["receipts"]}

    def decompose_claim(self, req):
        return {"assertions": [dict(a) for a in self._claims.get(req["case_id"], [])]}

    def classify_evidence(self, req):
        key = (req["case_id"], req["receipt"]["receipt_id"])
        if key not in self._rel:
            raise VerifierError(f"mock oracle has no fixture relation for {key}")
        relation, note = self._rel[key]
        return {"relation": relation, "rationale": f"[mock oracle: fixture relation] {note}"[:300], "quoted_span": note[:120]}


class ReplayVerifier:
    """Replays recorded responses from calls.jsonl. A miss is an error (recorded), never a silent pass."""

    mode = "replay"

    def __init__(self, calls_path):
        self._by_hash = {}
        models = set()
        with open(calls_path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    row = json.loads(line)
                    if row.get("ok"):
                        self._by_hash[row["prompt_sha256"]] = row["response"]
                        models.add(row.get("model", "?"))
        self.model = "replay:" + ",".join(sorted(models))

    def _get(self, name, req):
        h = prompt_hash(name, req)
        if h not in self._by_hash:
            raise VerifierError(f"replay miss for {name} prompt_sha256={h[:12]}")
        return self._by_hash[h]

    def decompose_claim(self, req):
        return self._get("M1", req)

    def classify_evidence(self, req):
        return self._get("M2", req)


def load_live_verifier(**kwargs):
    """Live Muse Spark adapter lives in app/muse_verify on top of app/api."""
    try:
        import importlib
        mod = importlib.import_module("app.muse_verify")
    except ImportError as exc:
        raise VerifierError(
            "live mode needs app/muse_verify (the Muse layer) exposing get_verifier(); "
            f"not importable: {exc}"
        )
    if not hasattr(mod, "get_verifier"):
        raise VerifierError("app.muse_verify has no get_verifier()")
    return mod.get_verifier(**kwargs)
