"""Ledger verification: python -m app.core.verify <run_dir>

1. Hash chain: every row_sha256 recomputes, every prev_sha256 links, seq is continuous.
2. Replay: recompute each case's final verdict from the ledger's own decompose/classify rows with the
   pure gate and compare with the last receipt_gate row and verdicts.jsonl.
Exit 0 = both pass, 1 = any failure.
"""
import json
import sys
from pathlib import Path

from . import contracts as C
from .engine import replay_verdicts
from .ledger import read_rows, verify_chain


def verify_run(run_dir):
    run_dir = Path(run_dir)
    ok, problems = verify_chain(run_dir / "ledger.jsonl")
    rows = [r for _, r in read_rows(run_dir / "ledger.jsonl")] if (run_dir / "ledger.jsonl").is_file() else []
    replayed = replay_verdicts(rows)
    gated = {}
    for r in rows:
        if r["step"] == "gate" and r["result"].get("pass") == C.PASS_RECEIPT:
            gated[r["case_id"]] = r["state_after"]
    recorded = {}
    vpath = run_dir / "verdicts.jsonl"
    if vpath.is_file():
        for line in vpath.read_text(encoding="utf-8").splitlines():
            if line.strip():
                o = json.loads(line)
                recorded[o["case_id"]] = o["verdict"]["state"]
    mism = [cid for cid in sorted(set(gated) | set(recorded))
            if not (replayed.get(cid) == gated.get(cid) == recorded.get(cid))]
    if mism:
        problems.append(f"replay mismatch for {mism}")
    return (ok and not mism), problems, {"rows": len(rows), "cases_replayed": len(replayed), "replay_mismatches": mism}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m app.core.verify <run_dir>", file=sys.stderr)
        return 2
    ok, problems, info = verify_run(argv[0])
    print(f"chain+replay {'PASS' if ok else 'FAIL'}  rows={info['rows']} cases_replayed={info['cases_replayed']}")
    for p in problems[:20]:
        print("  -", p)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
