#!/usr/bin/env python3
"""Validate the ClaimWatch sanitized demo data.

Three jobs, all offline:
  1. STRUCTURE  — every record conforms to claims.schema.json (jsonschema when
                  available, otherwise an equivalent built-in structural check).
  2. CONSISTENCY — verdict state / reason code / receipts / history / alert /
                  test expectation agree with each other (the state machine).
  3. SANITIZATION — no personal data, no credentials, no real hosts, no absolute
                  user paths, no key-shaped strings in any record.

Exit 0 = all checks pass. Exit 1 = at least one check failed (each failure is
printed with case_id + field). Exit 2 = files missing / unreadable.

Usage:
    python validate_demo_data.py [--dir PATH] [--quiet]

No network, no side effects, stdlib only (jsonschema is used if importable).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FILES = ("claims.jsonl", "bulk_cases.jsonl")
SCHEMA_FILE = "claims.schema.json"
MIN_CORE = 3
REQUIRED_FLIP = "DEMO-CASE-001"

ALLOWED_HOSTS = {"pricing.example.invalid", "docs.example.invalid", "bench.example.invalid"}
ALLOWED_SCHEMES = ("synthetic://demo-data/", "https://")

# --- sanitization patterns -------------------------------------------------
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
WIN_PATH = re.compile(r"\b[A-Za-z]:[\\/](?:Users|Documents|AppData)[\\/]", re.I)
HOME_PATH = re.compile(r"(?:^|[\s\"'(])/(?:home|Users)/[A-Za-z0-9._-]+/")
# key shapes: vendor prefixes, PEM blocks, bearer tokens, secret assignments
KEY_PREFIX = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{8,}|sk_live_[A-Za-z0-9]{8,}|rk_live_[A-Za-z0-9]{8,}"
    r"|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)
SECRET_ASSIGN = re.compile(
    r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|private[_-]?key|bearer)\b"
    r"\s*[:=]\s*\S+"
)
HEXLIKE = re.compile(r"\b[0-9a-fA-F]{32,}\b")
REAL_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{2,15}\b")
PSEUDONYM_OK = re.compile(r"^@?(?:demo_|synthetic_)", re.I)


def iter_strings(node, path=""):
    """Yield (path, value) for every string, tracking the JSON field name."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from iter_strings(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from iter_strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        yield path, node


def load_jsonl(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append((n, json.loads(line)))
            except json.JSONDecodeError as e:
                raise SystemExit(f"FAIL {os.path.basename(path)}:{n} not valid JSON: {e}")
    return rows


# --- 1. structure ----------------------------------------------------------
def validate_structure(record, schema, use_jsonschema):
    errors = []
    if use_jsonschema:
        import jsonschema

        validator = jsonschema.Draft7Validator(schema)
        for err in sorted(validator.iter_errors(record), key=lambda e: list(e.path)):
            loc = ".".join(str(p) for p in err.path) or "<root>"
            errors.append(f"{loc}: {err.message}")
        return errors
    return structural_fallback(record)


def structural_fallback(record):
    """Minimal shape check used when jsonschema is not installed."""
    errors = []
    required = [
        "case_id", "schema_version", "suite", "run_id", "title", "source", "claims",
        "receipts", "evidence_checklist", "verdict", "history", "alert",
        "engine_expectation", "notes",
    ]
    for k in required:
        if k not in record:
            errors.append(f"missing required field {k}")
    if record.get("schema_version") != "1.0.0":
        errors.append("schema_version must be 1.0.0")
    if record.get("suite") not in ("core_demo", "bulk", "adversarial"):
        errors.append("suite not in enum")
    src = record.get("source") or {}
    if src.get("distribution") != "synthetic":
        errors.append("source.distribution must be synthetic")
    ver = record.get("verdict") or {}
    if ver.get("state") not in ("SUBSTANCE", "UNVERIFIED", "REFUTED", "PENDING"):
        errors.append("verdict.state not in enum")
    if not isinstance(ver.get("confidence"), (int, float)) or not 0 <= ver["confidence"] <= 1:
        errors.append("verdict.confidence must be 0..1")
    for r in record.get("receipts") or []:
        if r.get("relation") not in ("supports", "contradicts", "insufficient"):
            errors.append(f"receipt {r.get('receipt_id')} relation not in enum")
        if not re.fullmatch(r"[a-f0-9]{64}", str(r.get("artifact_sha256", ""))):
            errors.append(f"receipt {r.get('receipt_id')} artifact_sha256 malformed")
    for h in record.get("history") or []:
        if h.get("state") not in ("SUBSTANCE", "UNVERIFIED", "REFUTED", "PENDING"):
            errors.append("history.state not in enum")
    return errors


# --- 2. consistency --------------------------------------------------------
def check_consistency(record):
    errors = []
    cid = record.get("case_id", "?")
    verdict = record.get("verdict") or {}
    state = verdict.get("state")
    reason = verdict.get("reason_code")
    receipts = record.get("receipts") or []
    history = record.get("history") or []
    alert = record.get("alert") or {}
    exp = record.get("engine_expectation") or {}
    rels = [r.get("relation") for r in receipts]
    ids = {r.get("receipt_id") for r in receipts}

    # history terminal state must equal the verdict, chronology strictly ordered
    if history:
        if history[-1].get("state") != state:
            errors.append(
                f"{cid}: history ends in {history[-1].get('state')} but verdict is {state}"
            )
        stamps = [h.get("at") for h in history]
        if stamps != sorted(stamps):
            errors.append(f"{cid}: history timestamps out of order")
        for prev, cur in zip(history, history[1:]):
            if prev.get("state") == cur.get("state"):
                errors.append(f"{cid}: history has a no-op state repeat ({cur.get('state')})")
    else:
        errors.append(f"{cid}: history is empty")

    # evidence -> state machine
    if state == "SUBSTANCE":
        if "contradicts" in rels:
            errors.append(f"{cid}: SUBSTANCE but a receipt contradicts")
        if not receipts or "supports" not in rels:
            errors.append(f"{cid}: SUBSTANCE with no supporting receipt")
        if reason != "EVIDENCE_SUFFICIENT":
            errors.append(f"{cid}: SUBSTANCE must carry reason EVIDENCE_SUFFICIENT")
    if state == "REFUTED":
        if "contradicts" not in rels:
            errors.append(f"{cid}: REFUTED without a contradicting receipt (absence is not refutation)")
        if reason != "EVIDENCE_CONTRADICTS" and reason != "TEMPORAL_MISMATCH" and reason != "SCOPE_MISMATCH":
            errors.append(f"{cid}: REFUTED carries a non-refuting reason {reason}")
    if state == "UNVERIFIED":
        if "contradicts" in rels:
            errors.append(f"{cid}: UNVERIFIED but a receipt contradicts (must be REFUTED)")
        if reason == "EVIDENCE_SUFFICIENT":
            errors.append(f"{cid}: UNVERIFIED with reason EVIDENCE_SUFFICIENT")
    if state == "PENDING":
        if reason not in ("RUN_EMPTY", "RUN_PARTIAL", "SOURCE_UNREACHABLE"):
            errors.append(f"{cid}: PENDING must carry RUN_EMPTY/RUN_PARTIAL/SOURCE_UNREACHABLE, got {reason}")
        if verdict.get("confidence") != 0:
            errors.append(f"{cid}: PENDING confidence must be 0")

    # every cited evidence id must exist
    for item in record.get("evidence_checklist") or []:
        for ev in item.get("evidence_ids") or []:
            if ev not in ids:
                errors.append(f"{cid}: checklist cites missing receipt {ev}")
    for h in history:
        for ev in h.get("evidence_ids") or []:
            if ev not in ids:
                errors.append(f"{cid}: history cites missing receipt {ev}")

    # alert agreement
    firing = bool(alert.get("should_fire"))
    if firing and alert.get("rule_id") == "none":
        errors.append(f"{cid}: alert fires with rule_id none")
    if not firing and alert.get("rule_id") != "none":
        errors.append(f"{cid}: alert rule {alert.get('rule_id')} set but should_fire=false")
    if firing and alert.get("severity") == "none":
        errors.append(f"{cid}: firing alert has severity none")
    if not firing and alert.get("severity") != "none":
        errors.append(f"{cid}: non-firing alert has severity {alert.get('severity')}")
    if state == "REFUTED" and alert.get("rule_id") not in ("A4_REFUTED", "none"):
        errors.append(f"{cid}: REFUTED should raise A4_REFUTED, got {alert.get('rule_id')}")

    # the record's own test expectation must match its verdict
    if exp.get("expected_state") != state:
        errors.append(f"{cid}: expected_state {exp.get('expected_state')} != verdict {state}")
    if bool(exp.get("expected_alert")) != firing:
        errors.append(f"{cid}: expected_alert != alert.should_fire")
    for c in exp.get("must_cite") or []:
        if c not in ids:
            errors.append(f"{cid}: must_cite references missing receipt {c}")

    return errors


# --- 3. sanitization -------------------------------------------------------
def check_sanitization(record):
    errors = []
    cid = record.get("case_id", "?")
    for path, value in iter_strings(record):
        # hashes are the one place long hex is legitimate
        if path.endswith("artifact_sha256"):
            if not re.fullmatch(r"[a-f0-9]{64}", value):
                errors.append(f"{cid}: {path} is not a lowercase sha256")
            continue

        if EMAIL.search(value):
            errors.append(f"{cid}: {path} contains an email address")
        if SSN.search(value):
            errors.append(f"{cid}: {path} contains an SSN-shaped value")
        if PHONE.search(value):
            errors.append(f"{cid}: {path} contains a phone-number-shaped value")
        if CARD.search(value) and not re.search(r"\d{4}-\d{2}-\d{2}", value):
            errors.append(f"{cid}: {path} contains a card-number-shaped value")
        if WIN_PATH.search(value) or HOME_PATH.search(value):
            errors.append(f"{cid}: {path} contains an absolute user path")
        if KEY_PREFIX.search(value):
            errors.append(f"{cid}: {path} contains a credential prefix")
        if SECRET_ASSIGN.search(value):
            errors.append(f"{cid}: {path} looks like a secret assignment")
        if HEXLIKE.search(value):
            errors.append(f"{cid}: {path} contains a 32+ char hex string outside a hash field")
        if IPV4.search(value):
            for ip in IPV4.findall(value):
                if not ip.startswith("192.0.2.") and not ip.startswith("198.51.100.") and not ip.startswith("203.0.113."):
                    errors.append(f"{cid}: {path} contains a non-documentation IP {ip}")
        for handle in REAL_HANDLE.findall(value):
            if not PSEUDONYM_OK.match(handle):
                errors.append(f"{cid}: {path} contains a non-pseudonymous handle {handle}")
        for url in re.findall(r"https?://[^\s\"')]+", value):
            host = url.split("://", 1)[1].split("/")[0].split(":")[0]
            if host not in ALLOWED_HOSTS:
                errors.append(f"{cid}: {path} references a non-synthetic host {host}")

    # source refs must stay inside the synthetic namespace
    src = record.get("source") or {}
    if not str(src.get("ref", "")).startswith("synthetic://demo-data/"):
        errors.append(f"{cid}: source.ref is outside synthetic://demo-data/")
    for r in record.get("receipts") or []:
        ref = str(r.get("ref", ""))
        if not (ref.startswith("synthetic://demo-data/") or ref.startswith(ALLOWED_SCHEMES)):
            errors.append(f"{cid}: receipt {r.get('receipt_id')} ref not allowlisted ({ref})")
    return errors


# --- flip-case contract ----------------------------------------------------
def check_flip_case(record):
    errors = []
    if record.get("case_id") != REQUIRED_FLIP:
        return errors
    text = " ".join(a.get("text", "") for a in record.get("claims") or [])
    if "400" not in text or "25" not in text:
        errors.append(f"{REQUIRED_FLIP}: must carry the 400 -> 25 figure pair, got: {text!r}")
    states = [h.get("state") for h in record.get("history") or []]
    if "SUBSTANCE" not in states or "UNVERIFIED" not in states:
        errors.append(f"{REQUIRED_FLIP}: history must flip SUBSTANCE -> UNVERIFIED, got {states}")
    if (record.get("verdict") or {}).get("state") != "UNVERIFIED":
        errors.append(f"{REQUIRED_FLIP}: final verdict must be UNVERIFIED (not REFUTED)")
    if (record.get("alert") or {}).get("rule_id") != "A1_VERDICT_DOWNGRADE":
        errors.append(f"{REQUIRED_FLIP}: must raise A1_VERDICT_DOWNGRADE")
    return errors


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=HERE)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    schema_path = os.path.join(args.dir, SCHEMA_FILE)
    try:
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)
    except OSError as e:
        print(f"FAIL cannot read {schema_path}: {e}")
        return 2
    except json.JSONDecodeError as e:
        print(f"FAIL {SCHEMA_FILE} is not valid JSON: {e}")
        return 2

    try:
        import jsonschema  # noqa: F401

        use_jsonschema = True
    except Exception:
        use_jsonschema = False

    records = []
    for name in FILES:
        path = os.path.join(args.dir, name)
        if not os.path.isfile(path):
            print(f"FAIL missing {name}")
            return 2
        for line_no, rec in load_jsonl(path):
            rec["__file__"] = name
            rec["__line__"] = line_no
            records.append(rec)

    failures = []
    seen = set()
    for rec in records:
        plain = {k: v for k, v in rec.items() if not k.startswith("__")}
        if plain.get("case_id") in seen:
            failures.append(f"{plain.get('case_id')}: duplicate case_id")
        seen.add(plain.get("case_id"))

        for e in validate_structure(plain, schema, use_jsonschema):
            failures.append(f"{plain.get('case_id')}: SCHEMA {e}")
        for e in check_consistency(plain):
            failures.append(f"CONSISTENCY {e}")
        for e in check_sanitization(plain):
            failures.append(f"SANITIZATION {e}")
        for e in check_flip_case(plain):
            failures.append(f"FLIP {e}")

    core = [r for r in records if r.get("suite") == "core_demo"]
    if len(core) < MIN_CORE:
        failures.append(f"SUITE only {len(core)} core_demo cases, need >= {MIN_CORE}")
    if REQUIRED_FLIP not in seen:
        failures.append(f"SUITE required demo case {REQUIRED_FLIP} is missing")

    states = sorted({r["verdict"]["state"] for r in records})
    if len(states) < 4:
        failures.append(f"SUITE verdict states covered {states}, need all four")

    counts = {}
    for r in records:
        counts[r.get("suite")] = counts.get(r.get("suite"), 0) + 1
    alerley = sum(1 for r in records if (r.get("alert") or {}).get("should_fire"))

    if not args.quiet or failures:
        print(f"dir        : {os.path.abspath(args.dir)}")
        print(f"jsonschema : {'yes' if use_jsonschema else 'no (structural fallback)'}")
        print(f"records    : {len(records)}  " + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        print(f"states     : {', '.join(states)}")
        print(f"alerts     : {alerley} fire / {len(records) - alerley} silent")

    if failures:
        print(f"\nRESULT: FAIL — {len(failures)} problem(s)")
        for f in failures:
            print("  - " + f)
        return 1
    print(f"\nRESULT: PASS — {len(records)} records, schema + consistency + sanitization clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
