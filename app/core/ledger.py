"""F5 receipt ledger: append-only JSONL, one row per step, sha256 hash chain.

row_sha256 = sha256(canonical(row without row_sha256)); prev_sha256 links each row to the one before;
the first row links to GENESIS (64 zeros). Editing, deleting or reordering any row breaks verify_chain().
"""
import hashlib
import json
from datetime import datetime, timezone

GENESIS = "0" * 64


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def row_hash(row):
    body = {k: v for k, v in row.items() if k != "row_sha256"}
    return sha256_hex(canonical(body))


class Ledger:
    def __init__(self, path, run_id, clock=utc_now):
        self.path = path
        self.run_id = run_id
        self.clock = clock
        self.seq = 0
        self.prev = GENESIS
        self.rows = []
        # 'x' = create exclusively: a ledger is never reopened for rewrite.
        self._fh = open(path, "x", encoding="utf-8", newline="\n")

    def append(self, step, case_id=None, assertion_id=None, receipt_id=None, call=None, result=None,
               state_before=None, state_after=None, reason_code=None):
        self.seq += 1
        row = {
            "seq": self.seq, "run_id": self.run_id, "ts": self.clock(), "step": step,
            "case_id": case_id, "assertion_id": assertion_id, "receipt_id": receipt_id,
            "call": call, "result": result if result is not None else {},
            "state_before": state_before, "state_after": state_after, "reason_code": reason_code,
            "prev_sha256": self.prev,
        }
        row["row_sha256"] = row_hash(row)
        self._fh.write(canonical(row) + "\n")
        self._fh.flush()
        self.prev = row["row_sha256"]
        self.rows.append(row)
        return row

    def close(self):
        self._fh.close()


def read_rows(path):
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if line.strip():
                rows.append((n, json.loads(line)))
    return rows


def verify_chain(path):
    """Return (ok, problems). Checks seq continuity, prev links and every row hash."""
    problems = []
    prev, expect_seq = GENESIS, 1
    try:
        rows = read_rows(path)
    except (OSError, json.JSONDecodeError) as exc:
        return False, [f"unreadable ledger: {exc}"]
    if not rows:
        return False, ["ledger has zero rows (I5)"]
    for line_no, row in rows:
        if row.get("seq") != expect_seq:
            problems.append(f"line {line_no}: seq {row.get('seq')} != expected {expect_seq}")
        if row.get("prev_sha256") != prev:
            problems.append(f"line {line_no}: prev_sha256 does not link to previous row")
        if row_hash(row) != row.get("row_sha256"):
            problems.append(f"line {line_no}: row_sha256 mismatch (row was edited)")
        prev, expect_seq = row.get("row_sha256"), expect_seq + 1
    return (not problems), problems
