"""Tests for the F8 dashboard: py -B -m unittest app.ui.test_ui -v   (run from the repo root)

Reads the committed app/core runs (never writes there); builds into a temp dir.
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

sys.dont_write_bytecode = True

from app.ui import build_dashboard as B  # noqa: E402

RUNS_ROOT = B.REPO_ROOT / "app" / "core" / "runs"
RUN_IDS = ["RUN-CORE-003-empty", "RUN-CORE-001-mock", "RUN-CORE-002-replay", "RUN-CORE-004-all-mock"]
VALIDATOR = B.REPO_ROOT / "demo_data" / "validate_demo_data.py"


class _Collect(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.hrefs, self.badges = set(), [], []
        self._badge, self._depth = None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "a" and a.get("href", "").startswith("#"):
            self.hrefs.append(a["href"][1:])
        if self._badge is not None:
            self._depth += 1
        elif "badge" in (a.get("class") or "").split():
            self._badge, self._depth = {"href": a.get("href"), "text": ""}, 1

    def handle_data(self, data):
        if self._badge is not None:
            self._badge["text"] += data

    def handle_endtag(self, tag):
        if self._badge is not None:
            self._depth -= 1
            if self._depth == 0:
                self.badges.append(self._badge)
                self._badge = None


def _load_validator():
    import importlib.util
    spec = importlib.util.spec_from_file_location("demo_data_validator", VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runs = [B.load_run(RUNS_ROOT / r) for r in RUN_IDS]
        cls.idx = {r["summary"]["run_id"]: B.RunIndex(r) for r in cls.runs}
        cls.html = B.build_html(cls.runs)
        cls.doc = _Collect()
        cls.doc.feed(cls.html)

    # ---- links: every badge links to the ledger row that produced it (F8)
    def test_every_internal_link_resolves(self):
        missing = sorted({h for h in self.doc.hrefs if h not in self.doc.ids})
        self.assertEqual(missing, [], f"dangling anchors: {missing[:10]}")

    def test_every_ledger_row_has_an_anchor(self):
        for rid, ix in self.idx.items():
            for r in ix.run["ledger"]:
                self.assertIn(f"{rid}-L{r['seq']}", self.doc.ids)

    def test_final_verdict_badges_link_to_their_receipt_gate_row(self):
        for rid, ix in self.idx.items():
            for v in ix.run["verdicts"]:
                cid = v["case_id"]
                m = re.search(rf'id="{re.escape(rid)}-case-{re.escape(cid)}"><summary><b>[^<]*</b> '
                              rf'<a class="badge [a-z]+" href="#([^"]+)"', self.html)
                self.assertIsNotNone(m, f"{rid} {cid}: case summary badge not linked")
                seq = int(m.group(1).rsplit("-L", 1)[1])
                row = next(r for r in ix.run["ledger"] if r["seq"] == seq)
                self.assertEqual((row["step"], row["case_id"], row["result"].get("pass")), ("gate", cid, "receipt_gate"))
                self.assertEqual(row["state_after"], v["verdict"]["state"])

    def test_history_chips_link_to_their_own_gate_pass(self):
        ix = self.idx["RUN-CORE-001-mock"]
        naive = ix.gate[("DEMO-CASE-001", "naive_baseline")]
        final = ix.gate[("DEMO-CASE-001", "receipt_gate")]
        self.assertEqual((naive["state_after"], final["state_after"]), ("SUBSTANCE", "UNVERIFIED"))
        self.assertIn(f'href="#RUN-CORE-001-mock-L{naive["seq"]}"', self.html)
        self.assertIn(f'href="#RUN-CORE-001-mock-L{final["seq"]}"', self.html)
        self.assertIn("naive baseline: what a confident harness would report (recorded, never trusted)", self.html)

    def test_all_badges_carry_text_and_ledger_links_point_at_rows(self):
        self.assertGreater(len(self.doc.badges), 100)
        for b in self.doc.badges:
            label = re.sub(r"[\u2714\u2716\u25b2\u25cb\u25c6?\s]", "", b["text"])
            self.assertTrue(label, f"color/glyph-only badge: {b}")  # never color-only
            if b["href"]:
                self.assertRegex(b["href"], r"^#.+-L\d+$")
                self.assertIn(b["href"][1:], self.doc.ids)

    # ---- demo beats
    def test_empty_run_is_failed_with_linked_a2(self):
        ix = self.idx["RUN-CORE-003-empty"]
        self.assertEqual(ix.run["summary"]["status"], "FAILED")
        self.assertEqual(ix.run_end["seq"], 2)
        sec = self._section("RUN-CORE-003-empty")
        self.assertIn('href="#RUN-CORE-003-empty-L2"', sec)          # status badge -> run_end
        self.assertIn('href="#RUN-CORE-003-empty-L1"', sec)          # A2 -> alert row seq 1
        self.assertIn('FAILED, not "success"', sec)
        self.assertIn("A2_EMPTY_RUN", sec)

    def test_flip_case_shows_no_receipt_for_a3(self):
        sec = self._section("RUN-CORE-001-mock")
        case = sec.split('id="RUN-CORE-001-mock-case-DEMO-CASE-001"', 1)[1].split("</details>", 1)[0]
        a3 = case.split("<b>A3</b>", 1)[1].split('<div class="assertion">', 1)[0]
        self.assertIn("<b>no receipt</b>", a3)
        self.assertIn("no supporting receipt", a3)
        for rid in ("R3", "R4", "R5"):
            self.assertIn(f"<b>{rid}</b>", case)
        self.assertIn("A1_VERDICT_DOWNGRADE", case.split("</summary>")[0] + sec)

    def test_mock_runs_never_call_counts_muse_spark(self):
        for rid, ix in self.idx.items():
            self.assertFalse(ix.run["summary"]["muse_spark_called"])
            sec = self._section(rid)
            self.assertIn("no Muse Spark call was made", sec)
            self.assertNotIn("<th>Muse Spark calls</th>", sec)
            if ix.run["verdicts"]:
                self.assertIn("<th>verifier calls (mock, not Muse Spark)</th>", sec)

    def test_bulk_grade_is_measured_and_every_miss_listed(self):
        sec = self._section("RUN-CORE-004-all-mock")
        g = self.idx["RUN-CORE-004-all-mock"].run["summary"]["grade"]
        self.assertIn(f"<b>{g['pass']}</b> / {g['cases']}", sec)
        for cid in g["misses"] + g["rule_misses"]:
            self.assertIn(f'href="#RUN-CORE-004-all-mock-case-{cid}"', sec.split("Every miss, listed")[1].split("</ul>")[0])

    def test_build_time_verify_passes_on_all_runs(self):
        for r in self.runs:
            self.assertTrue(r["verify"]["ok"], r["verify"]["problems"])

    # ---- safety
    def test_script_embedding_cannot_break_out(self):
        s = B.json_for_script(['{"x":"</script><script>alert(1)</script>"}', "<!-- c"])
        self.assertNotIn("</script", s)
        self.assertNotIn("<!--", s)
        self.assertEqual(json.loads(s), ['{"x":"</script><script>alert(1)</script>"}', "<!-- c"])

    def test_no_paths_or_hostnames_in_output(self):
        for bad in (str(B.REPO_ROOT), "E:\\", "C:\\"):
            self.assertNotIn(bad, self.html)

    def test_sanitization_sweep_on_output(self):
        v = _load_validator()
        # sweep visible text + embedded ledger JSON; drop the page's own CSS/JS code (e.g. "@media" is not a handle)
        text = re.sub(r"<style>.*?</style>", " ", self.html, flags=re.S)
        text = re.sub(r"<script>.*?</script>", " ", text, flags=re.S)
        self.assertIn("application/json", text)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\b[0-9a-f]{64}\b", "<sha256>", text)       # hash fields are allowed to hold hex
        hits = [e for e in v.check_sanitization({"case_id": "dashboard.html", "x": text})
                if "source.ref is outside" not in e]
        self.assertEqual(hits, [])

    def test_sanitization_negative_control(self):
        v = _load_validator()
        poisoned = "see C:\\Users\\someone\\x and mail me at a.person@corp-mail.com"
        hits = [e for e in v.check_sanitization({"case_id": "dashboard.html", "x": poisoned})
                if "source.ref is outside" not in e]
        self.assertGreaterEqual(len(hits), 2)

    def test_missing_run_file_exits_2(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(B.main([d, "--out", str(Path(d) / "x.html")]), 2)

    def test_cli_writes_file(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "dash.html"
            self.assertEqual(B.main([str(RUNS_ROOT / "RUN-CORE-001-mock"), "--out", str(out)]), 0)
            self.assertIn("RUN-CORE-001-mock", out.read_text(encoding="utf-8"))

    # ---- the in-browser re-check runs the same JS as the page: positive + negative control under node
    @unittest.skipUnless(shutil.which("node"), "node not on PATH")
    def test_js_ledger_check_positive_and_negative(self):
        js = str(B.HERE / "ledger_check.js").replace("\\", "/")
        prog = ("const lc=require(process.argv[1]);const lines=JSON.parse(require('fs').readFileSync(0,'utf8'));"
                "lc.verifyLines(lines).then(r=>{process.stdout.write(JSON.stringify(r))})")
        for r in self.runs:
            out = subprocess.run(["node", "-e", prog, js], input=json.dumps(r["ledger_lines"]),
                                 capture_output=True, text=True, encoding="utf-8", timeout=60)
            res = json.loads(out.stdout)
            self.assertTrue(res["ok"], (r["summary"]["run_id"], res["problems"][:3]))
            self.assertEqual(res["rows"], len(r["ledger_lines"]))
        lines = list(self.runs[1]["ledger_lines"])
        lines[4] = lines[4].replace('"relation":"insufficient"', '"relation":"supports"', 1)
        self.assertNotEqual(lines[4], self.runs[1]["ledger_lines"][4])
        out = subprocess.run(["node", "-e", prog, js], input=json.dumps(lines),
                             capture_output=True, text=True, encoding="utf-8", timeout=60)
        res = json.loads(out.stdout)
        self.assertFalse(res["ok"])
        self.assertTrue(any("row_sha256 mismatch" in p for p in res["problems"]))

    def _section(self, rid):
        return self.html.split(f'<section class="run" id="run-{rid}"', 1)[1].split("</section>", 1)[0]


if __name__ == "__main__":
    unittest.main()
