"""The per-case pipeline: ingest row -> M1 decompose -> M2 classify -> naive pass -> receipt gate -> alerts.

Every step writes one ledger row. Every verifier call also writes one calls.jsonl row (request + response),
which is what replay mode reads back. The engine receives ingest.engine_view(record) - never the answer key.
"""
import json
import time

from . import contracts as C
from .alerts import case_alerts, normalize_claim
from .gate import build_history, naive_gate, receipt_gate
from .ledger import canonical, sha256_hex
from .verifier import NonRetryableTransportError, RetryableResponseError, prompt_hash, validate_m1, validate_m2


class CallCapExceeded(Exception):
    pass


class Engine:
    def __init__(self, verifier, ledger, calls_fh, call_cap=C.DEFAULT_CALL_CAP):
        self.v = verifier
        self.ledger = ledger
        self.calls_fh = calls_fh
        self.call_cap = call_cap
        self.calls = 0
        self.call_errors = 0
        self.cap_hit = False
        self.seen_claims = {}

    # ---- one verifier call; only response parsing/schema failures get one retry -------
    def _call(self, name, req, fn, validator, case_id):
        if self.calls >= self.call_cap:
            self.cap_hit = True
            raise CallCapExceeded(f"call cap {self.call_cap} reached")
        ph = prompt_hash(name, req)
        resp, err, attempts = None, None, 0
        t0 = time.perf_counter()
        for attempts in (1, 2):
            self.calls += 1
            retryable = False
            try:
                resp = fn(req)
                err = validator(resp)
                retryable = err is not None
            except RetryableResponseError as exc:
                resp, err, retryable = None, f"{type(exc).__name__}: {exc}", True
            except NonRetryableTransportError as exc:
                resp, err, retryable = None, f"{type(exc).__name__}: {exc}", False
            except Exception as exc:  # unknown adapter failures are recorded once, never retried
                resp, err = None, f"{type(exc).__name__}: {exc}"
            if err is None or not retryable or self.calls >= self.call_cap:
                break
        latency_ms = int((time.perf_counter() - t0) * 1000)
        ok = err is None
        if not ok:
            self.call_errors += 1
        call_id = f"{self.ledger.run_id}-C{self.calls:04d}"
        rsha = sha256_hex(canonical(resp)) if resp is not None else None
        self.calls_fh.write(canonical({
            "call_id": call_id, "name": name, "model": self.v.model, "mode": self.v.mode, "case_id": case_id,
            "prompt_sha256": ph, "response_sha256": rsha, "request": req, "response": resp,
            "ok": ok, "error": err, "attempts": attempts, "latency_ms": latency_ms,
        }) + "\n")
        self.calls_fh.flush()
        call = {"call_id": call_id, "name": name, "model": self.v.model, "mode": self.v.mode,
                "prompt_sha256": ph, "response_sha256": rsha, "latency_ms": latency_ms, "attempts": attempts}
        return (resp if ok else None), err, call

    # ---- the case pipeline -------------------------------------------------------------------
    def run_case(self, view):
        cid = view["case_id"]
        L = self.ledger
        norm = normalize_claim(view["claim_text"])
        dup_count = 0
        if norm:
            self.seen_claims[norm] = self.seen_claims.get(norm, 0) + 1
            dup_count = self.seen_claims[norm]
        L.append("ingest", case_id=cid, state_after=C.PENDING, result={
            "suite": view["suite"], "source_type": view["source"]["type"], "receipts_in": len(view["receipts"]),
            "claim_text_sha256": sha256_hex(view["claim_text"]), "duplicate_count": dup_count})

        assertions, relations, m1_failed = [], [], False
        if view["claim_text"].strip():
            req = {"case_id": cid, "claim_text": view["claim_text"], "source_type": view["source"]["type"]}
            try:
                resp, err, call = self._call("M1", req, self.v.decompose_claim, validate_m1, cid)
            except CallCapExceeded as exc:
                resp, err, call = None, str(exc), None
            m1_failed = resp is None
            assertions = resp["assertions"] if resp else []
            L.append("decompose", case_id=cid, call=call, result={
                "ok": not m1_failed, "error": err,
                "assertions": [{k: a[k] for k in ("assertion_id", "kind", "checkable", "text")} for a in assertions]})

            ids = {a["assertion_id"] for a in assertions}
            for rc in view["receipts"]:
                targets = [rc["supports_assertion"]] if rc["supports_assertion"] in ids else sorted(ids)
                for aid in targets:
                    a = next(x for x in assertions if x["assertion_id"] == aid)
                    req = {"case_id": cid, "assertion": {"assertion_id": aid, "text": a["text"], "kind": a["kind"]},
                           "receipt": {k: rc[k] for k in ("receipt_id", "kind", "ref", "note", "retrieved_at")}}
                    try:
                        resp, err, call = self._call("M2", req, self.v.classify_evidence, validate_m2, cid)
                    except CallCapExceeded as exc:
                        resp, err, call = None, str(exc), None
                    rel = resp["relation"] if resp else None
                    relations.append({"assertion_id": aid, "receipt_id": rc["receipt_id"], "relation": rel})
                    L.append("classify", case_id=cid, assertion_id=aid, receipt_id=rc["receipt_id"], call=call,
                             result={"relation": rel, "error": err,
                                     "rationale": (resp or {}).get("rationale"), "quoted_span": (resp or {}).get("quoted_span")})

        at = L.clock()
        if m1_failed:
            final = {"state": C.PENDING, "reason_code": C.RUN_PARTIAL, "evidence_ids": [], "unsupported": [],
                     "basis": "M1 decomposition failed; no decision without assertions"}
            passes = [(C.PASS_RECEIPT, final, at)]
        else:
            naive = naive_gate(assertions, relations)
            final = receipt_gate(assertions, relations)
            passes = [(C.PASS_NAIVE, naive, at), (C.PASS_RECEIPT, final, at)]

        state = C.PENDING
        for name, v, _ in passes:
            L.append("gate", case_id=cid, state_before=state, state_after=v["state"], reason_code=v["reason_code"],
                     result={"pass": name, "evidence_ids": v["evidence_ids"], "unsupported": v["unsupported"],
                             "basis": v["basis"], "inputs": {"assertions": [{"assertion_id": a["assertion_id"], "checkable": a["checkable"]} for a in assertions],
                                                             "relations": relations}})
            state = v["state"]
        history = build_history(passes)

        alerts = case_alerts(final, history, dup_count)
        for rule in alerts["fired"]:
            L.append("alert", case_id=cid, state_after=final["state"], reason_code=final["reason_code"],
                     result={"rule_id": rule, "severity": C.ALERT_SEVERITY[rule], "primary": rule == alerts["primary"]})

        return {
            "case_id": cid, "suite": view["suite"], "title": view["title"],
            "case_status": C.RUN_FAILED if final["reason_code"] == C.RUN_EMPTY else
                           (C.RUN_STATUS_PARTIAL if final["state"] == C.PENDING else C.RUN_SUCCESS),
            "verdict": {"state": final["state"], "display": C.DISPLAY_LABEL[final["state"]],
                        "reason_code": final["reason_code"], "evidence_ids": final["evidence_ids"],
                        "unsupported_assertions": final["unsupported"], "basis": final["basis"]},
            "history": history,
            "alert": {"should_fire": bool(alerts["fired"]), "rule_id": alerts["primary"],
                      "severity": alerts["severity"], "all_fired": alerts["fired"]},
            "assertions": len(assertions), "receipts": len(view["receipts"]),
            "muse_calls": sum(1 for r in L.rows if r["case_id"] == cid and r["call"]),
            "duplicate_count": dup_count,
        }


def replay_verdicts(ledger_rows):
    """Recompute every case's final verdict from the ledger's own decompose/classify rows (F5 replay check)."""
    by_case = {}
    for row in ledger_rows:
        cid = row.get("case_id")
        if row["step"] == "decompose":
            by_case.setdefault(cid, {"a": [], "r": [], "m1_ok": True})
            by_case[cid]["a"] = row["result"]["assertions"]
            by_case[cid]["m1_ok"] = row["result"]["ok"]
        elif row["step"] == "classify":
            by_case.setdefault(cid, {"a": [], "r": [], "m1_ok": True})["r"].append(
                {"assertion_id": row["assertion_id"], "receipt_id": row["receipt_id"], "relation": row["result"]["relation"]})
        elif row["step"] == "ingest":
            by_case.setdefault(cid, {"a": [], "r": [], "m1_ok": True})
    out = {}
    for cid, d in by_case.items():
        out[cid] = receipt_gate(d["a"], d["r"])["state"] if d["m1_ok"] else C.PENDING
    return out
