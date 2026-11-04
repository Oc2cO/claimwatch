"""F4 deterministic gate. Pure functions: no I/O, no network, no model calls.

Inputs are the M1 assertions and the M2 relations ONLY (never the fixture answer key).
    assertions: [{"assertion_id", "checkable", ...}]
    relations:  [{"assertion_id", "receipt_id", "relation"}]   relation None = the M2 call failed
Output: {"state", "reason_code", "evidence_ids", "unsupported", "basis"}

Two passes, both recorded in the ledger and the history:
    naive_gate   - the failure the product exists to catch: "some evidence checks out, so it's verified".
                   ANY supports receipt and no contradiction -> SUBSTANCE. It ignores assertions with no
                   support. Recorded as history so a later downgrade is visible; never the final word.
    receipt_gate - the real rule. Every checkable assertion needs >= 1 supports receipt.
Invariant I1: absence of evidence -> UNVERIFIED (or PENDING if the source could not be read), never REFUTED.
"""
from . import contracts as C


def _verdict(state, reason, evidence=(), unsupported=(), basis=""):
    return {"state": state, "reason_code": reason, "evidence_ids": sorted(set(evidence)),
            "unsupported": sorted(set(unsupported)), "basis": basis}


def _empty_or_contradicted(assertions, relations):
    if not assertions:
        return _verdict(C.PENDING, C.RUN_EMPTY, basis="no assertions to check: empty input is not a clean result")
    checkable = {a["assertion_id"] for a in assertions if a["checkable"]}
    contra = [r for r in relations if r["relation"] == C.CONTRADICTS and r["assertion_id"] in checkable]
    if contra:
        return _verdict(C.REFUTED, C.EVIDENCE_CONTRADICTS, [r["receipt_id"] for r in contra],
                        basis=f"{len(contra)} receipt(s) contradict a checkable assertion")
    return None


def receipt_gate(assertions, relations):
    early = _empty_or_contradicted(assertions, relations)
    if early:
        return early
    checkable = [a["assertion_id"] for a in assertions if a["checkable"]]
    if not checkable:
        return _verdict(C.UNVERIFIED, C.CLAIM_UNCHECKABLE, basis="no assertion is checkable")
    by_a = {aid: [r for r in relations if r["assertion_id"] == aid] for aid in checkable}
    supported = {aid for aid, rs in by_a.items() if any(r["relation"] == C.SUPPORTS for r in rs)}
    unsupported = [aid for aid in checkable if aid not in supported]
    if not unsupported:
        sup = [r["receipt_id"] for r in relations if r["relation"] == C.SUPPORTS]
        return _verdict(C.SUBSTANCE, C.EVIDENCE_SUFFICIENT, sup,
                        basis=f"all {len(checkable)} checkable assertion(s) have a supports receipt")
    failed = [aid for aid in unsupported if by_a[aid] and all(r["relation"] is None for r in by_a[aid])]
    cited = [r["receipt_id"] for aid in unsupported for r in by_a[aid] if r["relation"] is not None]
    if failed and len(failed) == len(unsupported) and not cited:
        return _verdict(C.PENDING, C.SOURCE_UNREACHABLE, unsupported=unsupported,
                        basis="the verifier could not read the evidence; no decision without evidence")
    return _verdict(C.UNVERIFIED, C.EVIDENCE_MISSING, cited, unsupported,
                    basis=f"{len(unsupported)} of {len(checkable)} checkable assertion(s) lack a supports receipt")


def naive_gate(assertions, relations):
    early = _empty_or_contradicted(assertions, relations)
    if early:
        return early
    sup = [r["receipt_id"] for r in relations if r["relation"] == C.SUPPORTS]
    if sup:
        return _verdict(C.SUBSTANCE, C.EVIDENCE_SUFFICIENT, sup,
                        basis="[naive baseline] some evidence checks out, unsupported assertions ignored")
    return _verdict(C.UNVERIFIED, C.EVIDENCE_MISSING, basis="[naive baseline] no supporting receipt")


def build_history(passes):
    """passes: [(pass_name, verdict, at)]. Append-only, no no-op repeats (I2). Always >= 1 entry."""
    hist = []
    for name, v, at in passes:
        if hist and hist[-1]["state"] == v["state"]:
            continue
        hist.append({"at": at, "pass": name, "state": v["state"], "reason_code": v["reason_code"],
                     "basis": [v["basis"]], "evidence_ids": v["evidence_ids"]})
    return hist
