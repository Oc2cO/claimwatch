"""Grader: the ONLY module that reads the fixture answer key (verdict/history/alert/engine_expectation).

Per demo_data/README s5: expected_state exact; alert fires iff expected; must_cite subset of the engine's
cited evidence; must_not wrong behaviours; must_preserve_history; expected_history_states; run_status_expected.
The primary alert rule is compared to the fixture's alert.rule_id and reported separately (rule_match).
"""
from . import contracts as C

STATE_TOKENS = set(C.STATES) | {C.RUN_SUCCESS, C.RUN_FAILED}


def grade_case(out, record):
    ee = record["engine_expectation"]
    cited = set(out["verdict"]["evidence_ids"])
    for h in out["history"]:
        cited |= set(h["evidence_ids"])
    checks = {
        "state": out["verdict"]["state"] == ee["expected_state"],
        "alert_fires": out["alert"]["should_fire"] == ee["expected_alert"],
        "must_cite": set(ee["must_cite"]) <= cited,
    }
    bad = [m for m in ee["must_not"] if m in STATE_TOKENS and m in (out["verdict"]["state"], out["case_status"])]
    checks["must_not"] = not bad
    if "expected_history_states" in ee:
        checks["history_states"] = [h["state"] for h in out["history"]] == ee["expected_history_states"]
    if ee.get("must_preserve_history"):
        checks["history_preserved"] = len(out["history"]) >= 2
    if "run_status_expected" in ee:
        checks["case_status"] = out["case_status"] == ee["run_status_expected"]
    rule_match = out["alert"]["rule_id"] == record["alert"]["rule_id"]
    reason_match = out["verdict"]["reason_code"] == record["verdict"]["reason_code"]
    return {
        "case_id": record["case_id"], "pass": all(checks.values()), "checks": checks,
        "rule_match": rule_match, "reason_match": reason_match,
        "expected": {"state": ee["expected_state"], "alert": record["alert"]["rule_id"], "reason": record["verdict"]["reason_code"],
                     "must_cite": ee["must_cite"]},
        "got": {"state": out["verdict"]["state"], "alert": out["alert"]["rule_id"], "reason": out["verdict"]["reason_code"],
                "cited": sorted(cited)},
        "must_not_violated": bad,
    }


def summarize(grades):
    n = len(grades)
    return {
        "cases": n,
        "pass": sum(g["pass"] for g in grades),
        "state_match": sum(g["checks"]["state"] for g in grades),
        "alert_fires_match": sum(g["checks"]["alert_fires"] for g in grades),
        "alert_rule_match": sum(g["rule_match"] for g in grades),
        "reason_code_match": sum(g["reason_match"] for g in grades),
        "misses": [g["case_id"] for g in grades if not g["pass"]],
        "rule_misses": [g["case_id"] for g in grades if not g["rule_match"]],
    }
