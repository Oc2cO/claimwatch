"""F8 dashboard builder: py -B -m app.ui.build_dashboard <run_dir> [<run_dir> ...] [--out app/ui/dashboard.html]

Reads one or more app/core run dirs (ledger.jsonl, calls.jsonl, alerts.jsonl, verdicts.jsonl, grades.json,
run_summary.json) and writes ONE self-contained static HTML file. It opens offline from file:// with no server,
no network and no build step, because every byte of data is rendered or embedded at build time
(browsers block fetch() of local files, so a page that "loads the dir" at view time would not work offline).

What it shows:
  * run status badge (FAILED / PARTIAL / SUCCESS), linked to the run_end ledger row; run-level alerts
  * honesty banner: mode, verifier model, and whether Muse Spark was actually called (run_summary.muse_spark_called)
  * integrity: build-time chain+replay result from app.core.verify, plus an in-browser WebCrypto re-check
  * measured grade vs the fixture answer key (run_summary.grade / grades.json), every miss listed
  * alerts panel, claims table, per-case history timeline, receipts per assertion, verifier calls
  * the full ledger, one anchor per row. Every badge links to the ledger row that produced it.

Status is never color-only: every badge carries its text label and a shape glyph.
The builder never embeds absolute paths, hostnames or signing metadata: only run ids and run contents.
Exit 0 = written, 2 = usage / missing run file.
"""
import argparse
import html
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # never drop __pycache__ into app/core (outside this lane's write path)

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]  # repo root
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.verify import verify_run  # noqa: E402  (read-only reuse of the core verifier)

RUN_FILES = ("ledger.jsonl", "calls.jsonl", "alerts.jsonl", "verdicts.jsonl", "grades.json", "run_summary.json")

# state -> (css class, glyph, fallback display label). Display text comes from verdicts.jsonl when present.
STATE_STYLE = {
    "SUBSTANCE": ("ok", "✔", "Verified (substance)"),
    "UNVERIFIED": ("warn", "▲", "Unverified"),
    "REFUTED": ("bad", "✖", "Refuted"),
    "PENDING": ("pend", "○", "Pending"),
}
STATUS_STYLE = {"SUCCESS": ("ok", "✔"), "PARTIAL": ("warn", "▲"), "FAILED": ("bad", "✖")}
RELATION_STYLE = {"supports": ("ok", "✔"), "contradicts": ("bad", "✖"), "insufficient": ("warn", "▲")}
SEVERITY_STYLE = {"critical": ("bad", "✖"), "high": ("bad", "◆"), "medium": ("warn", "▲"),
                  "low": ("pend", "○"), "none": ("pend", "○")}
PASS_LABEL = {
    "naive_baseline": "naive baseline: what a confident harness would report (recorded, never trusted)",
    "receipt_gate": "receipt gate: deterministic rule (the verdict)",
}


def esc(x):
    return html.escape("" if x is None else str(x), quote=True)


def _jsonl(path):
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def load_run(run_dir):
    run_dir = Path(run_dir)
    missing = [f for f in RUN_FILES if not (run_dir / f).is_file()]
    if missing:
        raise FileNotFoundError(f"{run_dir.name}: missing {', '.join(missing)}")
    ledger_lines = [l for l in (run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    ok, problems, info = verify_run(run_dir)
    return {
        "summary": json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8")),
        "grades": json.loads((run_dir / "grades.json").read_text(encoding="utf-8")),
        "ledger_lines": ledger_lines,
        "ledger": [json.loads(l) for l in ledger_lines],
        "calls": _jsonl(run_dir / "calls.jsonl"),
        "alerts": _jsonl(run_dir / "alerts.jsonl"),
        "verdicts": _jsonl(run_dir / "verdicts.jsonl"),
        "verify": {"ok": ok, "problems": problems[:20], "rows": info["rows"],
                   "cases_replayed": info["cases_replayed"], "replay_mismatches": info["replay_mismatches"]},
    }


# ---------------------------------------------------------------- index the ledger so badges can link to rows

class RunIndex:
    def __init__(self, run):
        self.run = run
        self.rid = run["summary"]["run_id"]
        self.gate = {}        # (case_id, pass) -> last gate row
        self.alert = {}       # (case_id|None, rule_id) -> alert row
        self.decompose = {}   # case_id -> decompose row
        self.classify = {}    # case_id -> [classify rows]
        self.ingest = {}      # case_id -> ingest row
        self.run_end = None
        for r in run["ledger"]:
            step, cid = r["step"], r.get("case_id")
            if step == "gate":
                self.gate[(cid, r["result"].get("pass"))] = r
            elif step == "alert":
                self.alert.setdefault((cid, r["result"].get("rule_id")), r)
            elif step == "decompose":
                self.decompose[cid] = r
            elif step == "classify":
                self.classify.setdefault(cid, []).append(r)
            elif step == "ingest":
                self.ingest.setdefault(cid, r)
            elif step == "run_end":
                self.run_end = r
        self.calls = {c["call_id"]: c for c in run["calls"]}
        self.grade_by_case = {g["case_id"]: g for g in run["grades"].get("cases", [])}

    def anchor(self, row):
        return f"{self.rid}-L{row['seq']}"

    def href(self, row):
        return f"#{self.anchor(row)}" if row else None


def badge(cls, glyph, text, href=None, title=None):
    t = f' title="{esc(title)}"' if title else ""
    inner = f'<span class="g" aria-hidden="true">{glyph}</span> {esc(text)}'
    if href:
        return f'<a class="badge {cls}" href="{esc(href)}"{t}>{inner}</a>'
    return f'<span class="badge {cls}"{t}>{inner}</span>'


def state_badge(state, display=None, href=None, title=None):
    cls, glyph, fallback = STATE_STYLE.get(state, ("pend", "?", state or "?"))
    return badge(cls, glyph, display or fallback, href, title)


def short(h, n=12):
    return esc((h or "")[:n])


# ---------------------------------------------------------------- per-run sections

def render_honesty(ix):
    s = ix.run["summary"]
    if s.get("muse_spark_called"):
        return (f'<div class="banner live"><b>LIVE run.</b> Verifier model <code>{esc(s.get("model"))}</code>, '
                f'mode <code>{esc(s.get("mode"))}</code>. Muse Spark calls are recorded in the ledger and listed per case.</div>')
    return (f'<div class="banner mock"><b>{esc(str(s.get("mode", "")).upper())} RUN: no Muse Spark call was made.</b> '
            f'Verifier = <code>{esc(s.get("model"))}</code> (returns the fixture\'s recorded relations). '
            'These receipts prove the pipeline: ingest validation, deterministic gate, hash-chained ledger, alerts, replay, grading. '
            'They do not prove Muse Spark\'s reading quality. Call counts below are <b>verifier calls</b>, not Muse Spark calls.</div>')


def render_status(ix):
    s = ix.run["summary"]
    st = s.get("status", "?")
    cls, glyph = STATUS_STYLE.get(st, ("pend", "?"))
    t = s.get("totals", {})
    out = ['<div class="cards">']
    out.append(f'<div class="card"><div class="k">Run status</div>'
               f'{badge(cls, glyph, st, ix.href(ix.run_end), "run_end ledger row")}'
               f'<div class="sub">suite <code>{esc(s.get("suite"))}</code> · mode <code>{esc(s.get("mode"))}</code></div></div>')
    for k in ("cases", "assertions", "receipts", "calls", "alerts", "call_errors"):
        label = "verifier calls" if k == "calls" else k.replace("_", " ")
        out.append(f'<div class="card"><div class="k">{esc(label)}</div><div class="v">{esc(t.get(k, 0))}</div></div>')
    out.append(f'<div class="card"><div class="k">ledger rows</div><div class="v">{esc(s.get("ledger_rows"))}</div></div>')
    out.append('</div>')
    vc = s.get("verdict_counts", {})
    out.append('<div class="counts">Verdict counts: ' + " ".join(
        state_badge(k) + f' <b>{esc(vc.get(k, 0))}</b>' for k in ("SUBSTANCE", "UNVERIFIED", "REFUTED", "PENDING")) + '</div>')
    if st == "FAILED":
        out.append('<p class="explain bad-text"><b>FAILED, not "success".</b> A run that processed nothing has no evidence to '
                   'report. A naive harness would print success here (invariant I5).</p>')
    elif st == "PARTIAL":
        out.append('<p class="explain">PARTIAL: at least one case is PENDING/FAILED, a verifier call errored, or the call cap '
                   'was hit. The run may not report SUCCESS.</p>')
    ra = s.get("run_alerts", [])
    if ra:
        out.append('<h3>Run-level alerts</h3><ul class="alerts">')
        for a in ra:
            row = ix.alert.get((None, a.get("rule_id")))
            c, g = SEVERITY_STYLE.get(a.get("severity"), ("pend", "?"))
            out.append(f'<li>{badge(c, g, a.get("rule_id"), ix.href(row), "alert ledger row")} '
                       f'<span class="sev">{esc(a.get("severity"))}</span> {esc(a.get("why"))}</li>')
        out.append('</ul>')
    return "\n".join(out)


def render_integrity(ix):
    v = ix.run["verify"]
    s = ix.run["summary"]
    cls, glyph, word = ("ok", "✔", "PASS") if v["ok"] else ("bad", "✖", "FAIL")
    probs = "".join(f"<li>{esc(p)}</li>" for p in v["problems"])
    return (f'<div class="integrity">'
            f'<div>Build-time <code>app.core.verify</code> (hash chain + gate replay): {badge(cls, glyph, word)} '
            f'rows={esc(v["rows"])} · cases replayed={esc(v["cases_replayed"])}</div>'
            f'{"<ul>" + probs + "</ul>" if probs else ""}'
            f'<div>In-browser WebCrypto re-check of every row_sha256: '
            f'<span class="jscheck" data-run="{esc(ix.rid)}"><span class="badge pend"><span class="g">○</span> not run (JavaScript off)</span></span></div>'
            f'<div class="sub">ledger head <code>{esc(s.get("ledger_head_sha256"))}</code></div></div>')


def render_grade(ix):
    s = ix.run["summary"]
    g = s.get("grade", {})
    n = g.get("cases", 0)
    if not n:
        return '<p class="sub">No cases graded (empty run).</p>'
    rows = [("Full case pass (state, alert fires, must_cite, must_not, history)", g.get("pass")),
            ("expected_state exact", g.get("state_match")),
            ("Alert fires iff expected", g.get("alert_fires_match")),
            ("Primary alert rule matches fixture", g.get("alert_rule_match")),
            ("reason_code matches fixture (reported, not a pass/fail check)", g.get("reason_code_match"))]
    out = ['<table class="grade"><tr><th>Measure (vs fixture answer key, read only by grade.py)</th><th>Measured</th></tr>']
    out += [f'<tr><td>{esc(k)}</td><td><b>{esc(v)}</b> / {esc(n)}</td></tr>' for k, v in rows]
    out.append('</table>')
    misses = g.get("misses", [])
    rule_misses = g.get("rule_misses", [])
    if misses or rule_misses:
        out.append('<p><b>Every miss, listed:</b></p><ul>')
        for m in misses:
            cid = m if isinstance(m, str) else m.get("case_id")
            gr = ix.grade_by_case.get(cid, {})
            out.append(f'<li><a href="#{esc(ix.rid)}-case-{esc(cid)}">{esc(cid)}</a>: expected '
                       f'{esc(gr.get("expected", {}).get("state"))}, got {esc(gr.get("got", {}).get("state"))}</li>')
        for cid in rule_misses:
            gr = ix.grade_by_case.get(cid, {})
            out.append(f'<li><a href="#{esc(ix.rid)}-case-{esc(cid)}">{esc(cid)}</a> (alert rule): expected '
                       f'{esc(gr.get("expected", {}).get("alert"))}, fired {esc(gr.get("got", {}).get("alert"))}</li>')
        out.append('</ul>')
    else:
        out.append('<p>No misses.</p>')
    return "\n".join(out)


def render_alerts(ix):
    al = ix.run["alerts"]
    if not al:
        return '<p class="sub">No per-claim alerts fired.</p>'
    by_rule = {}
    for a in al:
        by_rule[a.get("rule_id")] = by_rule.get(a.get("rule_id"), 0) + 1
    out = ['<p>By rule: ' + " · ".join(f'<code>{esc(k)}</code> <b>{esc(n)}</b>' for k, n in sorted(by_rule.items())) +
           f' <span class="sub">({len(al)} alert rows; one case may fire more than one rule)</span></p>',
           '<ul class="alerts">']
    for a in al:
        row = ix.alert.get((a.get("case_id"), a.get("rule_id")))
        c, g = SEVERITY_STYLE.get(a.get("severity"), ("pend", "?"))
        if a.get("scope") == "run" or not a.get("case_id"):
            out.append(f'<li>{badge(c, g, a.get("rule_id"), ix.href(row), "alert ledger row")} '
                       f'<span class="sev">{esc(a.get("severity"))} · run-level</span> {esc(a.get("why"))}</li>')
            continue
        prim = "primary" if a.get("primary") else "also fired"
        out.append(f'<li>{badge(c, g, a.get("rule_id"), ix.href(row), "alert ledger row")} '
                   f'<span class="sev">{esc(a.get("severity"))} · {prim}</span> '
                   f'<a href="#{esc(ix.rid)}-case-{esc(a.get("case_id"))}">{esc(a.get("case_id"))}</a></li>')
    out.append('</ul>')
    return "\n".join(out)


def history_chips(ix, v):
    cid = v["case_id"]
    chips = []
    for h in v.get("history", []):
        row = ix.gate.get((cid, h.get("pass")))
        chips.append(state_badge(h.get("state"), None, ix.href(row), PASS_LABEL.get(h.get("pass"), h.get("pass"))))
    return ' <span class="arrow">→</span> '.join(chips)


def calls_label(ix):
    return "Muse Spark calls" if ix.run["summary"].get("muse_spark_called") else "verifier calls (mock, not Muse Spark)"


def render_claims_table(ix):
    vs = ix.run["verdicts"]
    if not vs:
        return '<p class="sub">No claims were processed.</p>'
    out = [f'<table class="claims"><tr><th>Case</th><th>Suite</th><th>Claim</th><th>Verdict</th><th>History</th>'
           f'<th>Alert</th><th>{esc(calls_label(ix))}</th><th>Graded</th></tr>']
    for v in vs:
        cid = v["case_id"]
        vd = v["verdict"]
        final = ix.gate.get((cid, "receipt_gate"))
        al = v.get("alert", {})
        arow = ix.alert.get((cid, al.get("rule_id"))) if al.get("should_fire") else None
        if al.get("should_fire"):
            c, g = SEVERITY_STYLE.get(al.get("severity"), ("pend", "?"))
            alert_cell = badge(c, g, al.get("rule_id"), ix.href(arow), "alert ledger row")
        else:
            alert_cell = '<span class="sub">none</span>'
        gr = ix.grade_by_case.get(cid)
        graded = ("" if gr is None else
                  badge("ok", "✔", "pass") if gr.get("pass") else badge("bad", "✖", "miss"))
        out.append(
            f'<tr class="s-{esc(vd.get("state"))}"><td><a href="#{esc(ix.rid)}-case-{esc(cid)}">{esc(cid)}</a></td>'
            f'<td>{esc(v.get("suite"))}</td><td>{esc(v.get("title"))}</td>'
            f'<td>{state_badge(vd.get("state"), vd.get("display"), ix.href(final), "receipt_gate ledger row")}</td>'
            f'<td>{history_chips(ix, v)}</td><td>{alert_cell}</td>'
            f'<td class="num">{esc(v.get("muse_calls", 0))}</td><td>{graded}</td></tr>')
    out.append('</table>')
    return "\n".join(out)


def render_case(ix, v):
    cid = v["case_id"]
    vd = v["verdict"]
    final = ix.gate.get((cid, "receipt_gate"))
    out = [f'<details class="case" id="{esc(ix.rid)}-case-{esc(cid)}"><summary>'
           f'<b>{esc(cid)}</b> {state_badge(vd.get("state"), vd.get("display"), ix.href(final), "receipt_gate ledger row")} '
           f'{esc(v.get("title"))}</summary>']
    out.append(f'<p><b>Verdict:</b> {esc(vd.get("state"))} / <code>{esc(vd.get("reason_code"))}</code>. '
               f'{esc(vd.get("basis"))}. Cites: {esc(", ".join(vd.get("evidence_ids", [])) or "none")}. '
               f'Case status: <b>{esc(v.get("case_status"))}</b>.</p>')
    # history timeline
    out.append('<h4>History (append-only)</h4><ol class="timeline">')
    for h in v.get("history", []):
        row = ix.gate.get((cid, h.get("pass")))
        out.append(f'<li>{state_badge(h.get("state"), None, ix.href(row), "gate ledger row")} '
                   f'<span class="pass">{esc(PASS_LABEL.get(h.get("pass"), h.get("pass")))}</span><br>'
                   f'<code>{esc(h.get("reason_code"))}</code> · {esc("; ".join(h.get("basis", [])))} · '
                   f'evidence {esc(", ".join(h.get("evidence_ids", [])) or "none")}</li>')
    if final and not any(h.get("pass") == "receipt_gate" for h in v.get("history", [])):
        out.append(f'<li>{state_badge(final.get("state_after"), None, ix.href(final), "receipt_gate ledger row")} '
                   f'<span class="pass">{esc(PASS_LABEL["receipt_gate"])}</span>: same state, so no history row '
                   f'was appended</li>')
    out.append('</ol>')
    # receipts per assertion
    dec = ix.decompose.get(cid)
    assertions = (dec or {}).get("result", {}).get("assertions") or []
    cls_rows = ix.classify.get(cid, [])
    out.append('<h4>Receipts per assertion</h4>')
    if dec:
        call = dec.get("call") or {}
        out.append(f'<p class="sub">Decomposed by call <a href="{esc(ix.href(dec))}">{esc(call.get("call_id"))}</a> '
                   f'({esc(call.get("name"))}, model <code>{esc(call.get("model"))}</code>, mode {esc(call.get("mode"))})</p>')
    if not assertions:
        out.append('<p class="bad-text"><b>No assertions to check.</b> Empty input is not a clean result.</p>')
    unsupported = set(vd.get("unsupported_assertions", []))
    for a in assertions:
        aid = a.get("assertion_id")
        mine = [r for r in cls_rows if r.get("assertion_id") == aid]
        flag = (' <span class="badge warn"><span class="g">▲</span> no supporting receipt</span>'
                if aid in unsupported else "")
        out.append(f'<div class="assertion"><div><b>{esc(aid)}</b> <span class="sub">[{esc(a.get("kind"))}'
                   f'{"" if a.get("checkable", True) else ", not checkable"}]</span> {esc(a.get("text"))}{flag}</div>')
        if not mine:
            out.append('<div class="receipt none"><b>no receipt</b>: nothing was offered as evidence for this assertion</div>')
        for r in mine:
            res = r.get("result") or {}
            call = r.get("call") or {}
            req = (ix.calls.get(call.get("call_id")) or {}).get("request", {}).get("receipt", {})
            rel = res.get("relation")
            c, g = RELATION_STYLE.get(rel, ("pend", "?"))
            err = f' <span class="bad-text">error: {esc(res.get("error"))}</span>' if res.get("error") else ""
            out.append(
                f'<div class="receipt"><b>{esc(r.get("receipt_id"))}</b> '
                f'{badge(c, g, rel or "no relation", ix.href(r), "classify ledger row")} '
                f'<span class="sub">{esc(req.get("kind"))} · <code>{esc(req.get("ref"))}</code></span>{err}'
                f'<div class="note">{esc(req.get("note"))}</div>'
                f'<div class="rat">rationale: {esc(res.get("rationale"))}</div>'
                f'<div class="sub">call {esc(call.get("call_id"))} · {esc(call.get("name"))} · '
                f'prompt {short(call.get("prompt_sha256"))}… · response {short(call.get("response_sha256"))}…</div></div>')
        out.append('</div>')
    out.append(f'<p class="sub">{esc(calls_label(ix))} for this case: {esc(v.get("muse_calls", 0))}</p>')
    out.append('</details>')
    return "\n".join(out)


def describe_row(r):
    res = r.get("result") or {}
    step = r["step"]
    if step == "classify":
        return f'{r.get("assertion_id")} × {r.get("receipt_id")} → {res.get("relation")}'
    if step == "gate":
        return f'{res.get("pass")}: {r.get("state_before")} → {r.get("state_after")}'
    if step == "alert":
        return f'{res.get("rule_id")} ({res.get("severity")})'
    if step == "decompose":
        return f'{len(res.get("assertions") or [])} assertion(s)'
    if step == "ingest":
        return f'receipts in: {res.get("receipts_in")}'
    if step == "run_end":
        return f'status {res.get("status")}'
    return ""


def render_ledger(ix):
    out = ['<table class="ledger"><tr><th>seq</th><th>step</th><th>case</th><th>what</th><th>reason</th>'
           '<th>call</th><th>row_sha256</th><th>prev</th></tr>']
    for r in ix.run["ledger"]:
        call = r.get("call") or {}
        out.append(
            f'<tr id="{esc(ix.anchor(r))}"><td class="num"><a href="#{esc(ix.anchor(r))}">{esc(r["seq"])}</a></td>'
            f'<td>{esc(r["step"])}</td><td>{esc(r.get("case_id") or "run")}</td><td>{esc(describe_row(r))}</td>'
            f'<td><code>{esc(r.get("reason_code") or "")}</code></td>'
            f'<td>{esc(call.get("name") or "")} {esc(call.get("mode") or "")}</td>'
            f'<td><code title="{esc(r.get("row_sha256"))}">{short(r.get("row_sha256"), 16)}</code></td>'
            f'<td><code title="{esc(r.get("prev_sha256"))}">{short(r.get("prev_sha256"), 8)}</code></td></tr>')
    out.append('</table>')
    return "\n".join(out)


def render_run(ix):
    s = ix.run["summary"]
    rid = esc(ix.rid)
    parts = [f'<section class="run" id="run-{rid}" data-run="{rid}">',
             f'<h2>{rid} <span class="sub">started {esc(s.get("started"))} · finished {esc(s.get("finished"))}</span></h2>',
             render_honesty(ix), render_status(ix),
             '<h3>Integrity</h3>', render_integrity(ix),
             '<h3>Graded against the fixture (measured, no target asserted)</h3>', render_grade(ix),
             '<h3>Alerts</h3>', render_alerts(ix),
             '<h3>Claims</h3>', render_claims_table(ix),
             '<h3>Case detail: history and receipts per assertion</h3>',
             "\n".join(render_case(ix, v) for v in ix.run["verdicts"]) or '<p class="sub">No cases.</p>',
             f'<details class="ledgerwrap" id="{rid}-ledger"><summary><b>Receipt ledger</b> ({esc(len(ix.run["ledger"]))} rows, '
             f'append-only, sha256-chained). Every badge above links to a row here.</summary>',
             render_ledger(ix), '</details>',
             f'<script type="application/json" class="ledger-lines" data-run="{rid}">'
             f'{json_for_script(ix.run["ledger_lines"])}</script>',
             '</section>']
    return "\n".join(parts)


def json_for_script(obj):
    # "</script" or "<!--" would end/alter the <script> element; "<" only occurs inside JSON strings, where
    # "<" is the same value for both json.loads and JSON.parse.
    return json.dumps(obj, ensure_ascii=False).replace("<", "\\u003c")


def build_html(runs):
    indexes = [RunIndex(r) for r in runs]
    nav = " ".join(
        f'<a class="runlink" href="#run-{esc(ix.rid)}" data-run="{esc(ix.rid)}">{esc(ix.rid)} '
        f'<span class="sub">({esc(ix.run["summary"].get("suite"))}, {esc(ix.run["summary"].get("status"))})</span></a>'
        for ix in indexes)
    css = (HERE / "dashboard.css").read_text(encoding="utf-8")
    js_check = (HERE / "ledger_check.js").read_text(encoding="utf-8")
    js_page = (HERE / "dashboard.js").read_text(encoding="utf-8")
    body = "\n".join(render_run(ix) for ix in indexes)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Claim Watch: receipt ledger dashboard</title>
<style>{css}</style></head>
<body>
<header><h1>Claim Watch <span class="sub">AI work that shows its evidence, and says so when it has none.</span></h1>
<nav aria-label="runs">Runs: {nav}</nav>
<p class="sub">Static, offline page built from <code>app/core</code> run directories. Labels: <b>Verified (substance)</b> = SUBSTANCE,
every checkable assertion has a supporting receipt. <b>Unverified</b> = something lacks evidence, but nothing contradicts it
(absence of evidence never refutes). <b>Refuted</b> = a receipt contradicts. <b>Pending</b> = nothing could be checked.</p></header>
<main>
{body}
</main>
<script>{js_check}</script>
<script>{js_page}</script>
</body></html>
"""


def main(argv=None):
    p = argparse.ArgumentParser(description="Build the static F8 dashboard from app/core run dirs.")
    p.add_argument("run_dirs", nargs="+")
    p.add_argument("--out", default=str(HERE / "dashboard.html"))
    a = p.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        runs = [load_run(d) for d in a.run_dirs]
    except FileNotFoundError as e:
        print(f"build_dashboard: {e}", file=sys.stderr)
        return 2
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_html(runs), encoding="utf-8", newline="\n")
    for r in runs:
        v = r["verify"]
        print(f"{r['summary']['run_id']}: status={r['summary'].get('status')} rows={v['rows']} "
              f"verify={'PASS' if v['ok'] else 'FAIL'}")
    print(f"wrote {out.name} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
