"""F7 run harness / CLI.

    cd <repo root>
    python -m app.core.run --suite core_demo --mode mock            # writes app/core/runs/<run_id>/
    python -m app.core.run --suite empty --mode mock                # the Oct-20 shape: FAILED + A2, exit 1
    python -m app.core.run --suite core_demo --mode replay --replay-from app/core/runs/<run_id>
    python -m app.core.verify app/core/runs/<run_id>                # hash chain + replay check

Writes into the run dir (never overwrites: the dir must not exist):
    ledger.jsonl      one sha256-chained row per step
    calls.jsonl       every verifier call: request, response, hashes, latency (replay source)
    alerts.jsonl      every alert fired
    verdicts.jsonl    engine output per case (verdict, history, alert)
    grades.json       grader comparison against engine_expectation (answer key read ONLY here)
    run_summary.json  totals + status

Exit codes: 0 SUCCESS or PARTIAL · 1 FAILED (I5: zero claims or zero receipts) · 2 setup error · 3 ingest rejected a row.
"""
import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import contracts as C
from .engine import Engine
from .grade import grade_case, summarize
from .ingest import IngestError, engine_view, load_records, select_suite
from .ledger import Ledger, canonical, utc_now
from .verifier import MockVerifier, ReplayVerifier, VerifierError, load_live_verifier

CORE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = CORE_DIR.parents[1]          # repo root
DEFAULT_DATA = PROJECT_DIR / "demo_data"
DEFAULT_OUT = CORE_DIR / "runs"            # mission grant covers app/core/ only (see CORE_RUN.md)


def _rel(p):
    """Relative display path only: no absolute paths in artifacts."""
    try:
        return Path(p).resolve().relative_to(PROJECT_DIR).as_posix()
    except ValueError:
        return Path(p).name


def build_parser():
    ap = argparse.ArgumentParser(prog="python -m app.core.run", description="claim ingest -> Muse Spark verify -> receipt ledger")
    ap.add_argument("--data", default=str(DEFAULT_DATA), help="demo_data dir (claims.schema.json + *.jsonl)")
    ap.add_argument("--suite", default="core_demo", choices=["core_demo", "bulk", "adversarial", "all", "empty"])
    ap.add_argument("--mode", default="mock", choices=["mock", "replay", "live"])
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="parent dir for runs/<run_id>/")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--replay-from", default=None, help="run dir whose calls.jsonl is replayed (mode=replay)")
    ap.add_argument("--call-cap", type=int, default=C.DEFAULT_CALL_CAP)
    ap.add_argument("--quiet", action="store_true")
    return ap


def make_verifier(args, full_records):
    if args.mode == "mock":
        return MockVerifier(full_records)
    if args.mode == "replay":
        if not args.replay_from:
            raise VerifierError("--mode replay needs --replay-from <run_dir>")
        return ReplayVerifier(Path(args.replay_from) / "calls.jsonl")
    return load_live_verifier()


def main(argv=None):
    args = build_parser().parse_args(argv)
    say = (lambda *a: None) if args.quiet else print
    try:
        all_records, _prov = load_records(args.data)
    except IngestError as exc:
        print(f"INGEST REJECTED: {exc}", file=sys.stderr)
        return 3
    records = select_suite(all_records, args.suite)
    try:
        verifier = make_verifier(args, all_records)
    except VerifierError as exc:
        print(f"SETUP ERROR: {exc}", file=sys.stderr)
        return 2

    started = utc_now()
    run_id = args.run_id or f"RUN-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{args.suite}-{args.mode}"
    run_dir = Path(args.out) / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    ledger = Ledger(run_dir / "ledger.jsonl", run_id)
    outs, grades = [], []
    with open(run_dir / "calls.jsonl", "x", encoding="utf-8", newline="\n") as calls_fh:
        eng = Engine(verifier, ledger, calls_fh, call_cap=args.call_cap)
        for rec in records:
            out = eng.run_case(engine_view(rec))
            outs.append(out)
            grades.append(grade_case(out, rec))

    n_assert = sum(o["assertions"] for o in outs)
    n_rcpt = sum(o["receipts"] for o in outs)
    run_alerts = []
    if not outs or n_assert == 0:
        status = C.RUN_FAILED                                    # I5
        run_alerts.append({"rule_id": C.A2, "severity": C.ALERT_SEVERITY[C.A2], "scope": "run",
                           "why": f"run processed {len(outs)} case(s), {n_assert} assertion(s), {n_rcpt} receipt(s)"})
    elif eng.cap_hit or eng.call_errors or any(o["case_status"] != C.RUN_SUCCESS for o in outs):
        status = C.RUN_STATUS_PARTIAL
    else:
        status = C.RUN_SUCCESS
    if eng.cap_hit:
        run_alerts.append({"rule_id": "RUN_PARTIAL_CALL_CAP", "severity": "high", "scope": "run",
                           "why": f"call cap {args.call_cap} reached; remaining steps recorded RUN_PARTIAL"})
    for a in run_alerts:
        ledger.append("alert", state_after=None, reason_code=C.RUN_EMPTY if a["rule_id"] == C.A2 else C.RUN_PARTIAL,
                      result=dict(a, primary=True))

    verdict_counts = {s: sum(1 for o in outs if o["verdict"]["state"] == s) for s in C.STATES}
    case_alert_rows = [dict(case_id=o["case_id"], rule_id=r, severity=C.ALERT_SEVERITY[r], primary=(r == o["alert"]["rule_id"]))
                       for o in outs for r in o["alert"]["all_fired"]]
    totals = {"cases": len(outs), "assertions": n_assert, "receipts": n_rcpt, "calls": eng.calls,
              "call_errors": eng.call_errors, "alerts": len(case_alert_rows) + len(run_alerts)}
    end = ledger.append("run_end", result=dict(totals, status=status, verdict_counts=verdict_counts, mode=verifier.mode))
    ledger.close()

    with open(run_dir / "alerts.jsonl", "x", encoding="utf-8", newline="\n") as fh:
        for a in case_alert_rows + run_alerts:
            fh.write(canonical(a) + "\n")
    with open(run_dir / "verdicts.jsonl", "x", encoding="utf-8", newline="\n") as fh:
        for o in outs:
            fh.write(canonical(o) + "\n")
    gsum = summarize(grades)
    (run_dir / "grades.json").write_text(json.dumps({"summary": gsum, "cases": grades}, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    summary = {
        "run_id": run_id, "suite": args.suite, "mode": verifier.mode, "model": verifier.model,
        "live_request_attempted": verifier.mode == "live" and getattr(verifier, "request_attempts", 0) > 0,
        "muse_spark_called": verifier.mode == "live" and getattr(verifier, "successful_http_responses", 0) > 0,
        "data": _rel(args.data), "started": started, "finished": utc_now(), "status": status,
        "totals": totals, "verdict_counts": verdict_counts, "run_alerts": run_alerts,
        "ledger_rows": end["seq"], "ledger_head_sha256": end["row_sha256"], "grade": gsum,
    }
    (run_dir / "run_summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    say(f"run {run_id}  suite={args.suite}  mode={verifier.mode}  model={verifier.model}")
    for o, g in zip(outs, grades):
        hist = " -> ".join(h["state"] for h in o["history"])
        say(f"  {o['case_id']:<14} {o['verdict']['state']:<10} {o['verdict']['reason_code']:<20} alert={o['alert']['rule_id']:<25} "
            f"history[{hist}] cites={o['verdict']['evidence_ids']} grade={'PASS' if g['pass'] else 'MISS'}")
    for a in run_alerts:
        say(f"  RUN ALERT {a['rule_id']} ({a['severity']}): {a['why']}")
    say(f"STATUS {status}  cases={totals['cases']} assertions={n_assert} receipts={n_rcpt} calls={eng.calls} "
        f"ledger_rows={end['seq']}  grade {gsum['pass']}/{gsum['cases']} pass  -> {_rel(run_dir)}")
    return 1 if status == C.RUN_FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
