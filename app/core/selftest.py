"""Core self-tests (stdlib unittest, offline). Run from the repo root:  python -B -m unittest app.core.selftest -v

Lives under app/core/ because tests/ belongs to the API layer.
"""
import copy
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import urllib.error

from . import contracts as C
from .alerts import case_alerts
from .engine import Engine
from .gate import build_history, naive_gate, receipt_gate
from .ingest import IngestError, engine_view, load_records
from .ledger import Ledger, verify_chain
from .run import DEFAULT_DATA, main as run_main
from .verifier import MockVerifier, NonRetryableTransportError, VerifierError, validate_m1


class EngineRetryBoundaryTests(unittest.TestCase):
    """Offline cross-layer retry tests; FakeOpener is the only transport boundary."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    class FakeResponse:
        def __init__(self, payload, status=200):
            self.payload = payload
            self.status = status
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def getcode(self): return self.status
        def read(self, _limit): return self.payload

    class FakeOpener:
        def __init__(self, outcomes):
            self.outcomes = list(outcomes)
            self.opens = 0
        def open(self, _req, timeout):
            self.opens += 1
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    def _call_once(self, opener, budget):
        from app.api import HttpTransport
        from app.muse_verify import get_verifier
        t = HttpTransport("synthetic-test-key", max_requests=budget)
        v = get_verifier(transport=t, model=t.model)
        ledger = Ledger(self.tmp / f"ledger-{len(list(self.tmp.glob('ledger-*')))}.jsonl", "R")
        engine = Engine(v, ledger, io.StringIO(), call_cap=8)
        req = {"case_id": "T", "claim_text": "synthetic claim", "source_type": "synthetic"}
        try:
            with patch("app.api.urllib.request.build_opener", return_value=opener):
                result = engine._call("M1", req, v.decompose_claim, validate_m1, "T")
        finally:
            ledger.close()
        return t, engine, result

    @staticmethod
    def _response(content):
        doc = {"model": "muse-spark-1.3", "usage": {"prompt_tokens": 1, "completion_tokens": 1},
               "choices": [{"message": {"content": content}}]}
        return EngineRetryBoundaryTests.FakeResponse(json.dumps(doc).encode("utf-8"))

    def test_timeout_http_and_transport_errors_make_one_post(self):
        from app.api import LIVE_ENDPOINT
        errors = [TimeoutError("synthetic timeout"), OSError("synthetic offline"),
                  urllib.error.HTTPError(LIVE_ENDPOINT, 503, "synthetic", {}, None)]
        for i, error in enumerate(errors):
            with self.subTest(error=type(error).__name__):
                opener = self.FakeOpener([error])
                t, engine, (resp, err, call) = self._call_once(opener, budget=2)
                if isinstance(error, urllib.error.HTTPError):
                    error.close()
                self.assertEqual((opener.opens, t.calls), (1, 1))
                self.assertEqual(engine.calls, 1)
                self.assertEqual(call["attempts"], 1)
                self.assertIsNone(resp)
                self.assertIn("NonRetryableTransportError", err)

    def test_schema_failure_retries_once_with_remaining_budget(self):
        valid = json.dumps({"assertions": [{"assertion_id": "A1", "text": "synthetic",
                                             "kind": "existence", "checkable": True}]})
        opener = self.FakeOpener([self._response("not-json"), self._response(valid)])
        t, engine, (resp, err, call) = self._call_once(opener, budget=2)
        self.assertEqual((opener.opens, t.calls, engine.calls, call["attempts"]), (2, 2, 2, 2))
        self.assertIsNone(err)
        self.assertEqual(resp["assertions"][0]["assertion_id"], "A1")

    def test_schema_retry_stops_at_transport_budget(self):
        opener = self.FakeOpener([self._response("not-json")])
        t, engine, (resp, err, call) = self._call_once(opener, budget=1)
        self.assertEqual((opener.opens, t.calls, engine.calls, call["attempts"]), (1, 1, 2, 2))
        self.assertIsNone(resp)
        self.assertIn("NonRetryableTransportError", err)
from .verify import verify_run

A = lambda i, chk=True: {"assertion_id": i, "checkable": chk, "text": i, "kind": "quantitative"}
R = lambda a, r, rel: {"assertion_id": a, "receipt_id": r, "relation": rel}


class GateTests(unittest.TestCase):
    def test_substance(self):
        v = receipt_gate([A("A1"), A("A2")], [R("A1", "R1", "supports"), R("A2", "R2", "supports")])
        self.assertEqual((v["state"], v["reason_code"], v["evidence_ids"]), (C.SUBSTANCE, C.EVIDENCE_SUFFICIENT, ["R1", "R2"]))

    def test_unverified_partial_support(self):
        v = receipt_gate([A("A1"), A("A2")], [R("A1", "R1", "supports"), R("A2", "R2", "insufficient")])
        self.assertEqual((v["state"], v["unsupported"], v["evidence_ids"]), (C.UNVERIFIED, ["A2"], ["R2"]))

    def test_refuted(self):
        v = receipt_gate([A("A1")], [R("A1", "R1", "contradicts")])
        self.assertEqual((v["state"], v["reason_code"]), (C.REFUTED, C.EVIDENCE_CONTRADICTS))

    def test_pending_empty(self):
        self.assertEqual(receipt_gate([], [])["reason_code"], C.RUN_EMPTY)
        self.assertEqual(receipt_gate([], [])["state"], C.PENDING)

    def test_pending_source_unreachable(self):
        v = receipt_gate([A("A1")], [R("A1", "R1", None)])
        self.assertEqual((v["state"], v["reason_code"]), (C.PENDING, C.SOURCE_UNREACHABLE))

    def test_I1_absence_is_never_refuted(self):
        for rels in ([], [R("A1", "R1", "insufficient")], [R("A1", "R1", None)]):
            self.assertNotEqual(receipt_gate([A("A1")], rels)["state"], C.REFUTED)
            self.assertNotEqual(naive_gate([A("A1")], rels)["state"], C.REFUTED)

    def test_uncheckable(self):
        self.assertEqual(receipt_gate([A("A1", False)], [])["reason_code"], C.CLAIM_UNCHECKABLE)

    def test_contradiction_on_uncheckable_assertion_ignored(self):
        self.assertNotEqual(receipt_gate([A("A1", False)], [R("A1", "R1", "contradicts")])["state"], C.REFUTED)

    def test_naive_is_fooled_receipt_gate_is_not(self):
        a, r = [A("A1"), A("A2")], [R("A1", "R1", "supports")]
        self.assertEqual(naive_gate(a, r)["state"], C.SUBSTANCE)
        self.assertEqual(receipt_gate(a, r)["state"], C.UNVERIFIED)

    def test_history_append_only_no_repeats(self):
        s = receipt_gate([A("A1")], [R("A1", "R1", "supports")])
        u = receipt_gate([A("A1")], [])
        h = build_history([("p1", s, "t1"), ("p2", s, "t2"), ("p3", u, "t3")])
        self.assertEqual([x["state"] for x in h], [C.SUBSTANCE, C.UNVERIFIED])


class AlertTests(unittest.TestCase):
    def _h(self, *states):
        return [{"state": s} for s in states]

    def test_downgrade_fires_A1_primary(self):
        al = case_alerts({"state": C.UNVERIFIED, "reason_code": C.EVIDENCE_MISSING}, self._h(C.SUBSTANCE, C.UNVERIFIED), 1)
        self.assertEqual(al["primary"], C.A1)
        self.assertIn(C.A3, al["fired"])

    def test_empty_A2(self):
        self.assertEqual(case_alerts({"state": C.PENDING, "reason_code": C.RUN_EMPTY}, self._h(C.PENDING), 0)["primary"], C.A2)

    def test_refuted_A4(self):
        self.assertEqual(case_alerts({"state": C.REFUTED, "reason_code": C.EVIDENCE_CONTRADICTS}, self._h(C.REFUTED), 1)["primary"], C.A4)

    def test_substance_is_silent(self):
        al = case_alerts({"state": C.SUBSTANCE, "reason_code": C.EVIDENCE_SUFFICIENT}, self._h(C.SUBSTANCE), 1)
        self.assertEqual((al["fired"], al["primary"]), ([], C.NO_ALERT))

    def test_duplicate_flood_A5(self):
        al = case_alerts({"state": C.UNVERIFIED, "reason_code": C.EVIDENCE_MISSING}, self._h(C.UNVERIFIED), C.DUPLICATE_FLOOD_THRESHOLD)
        self.assertEqual(al["primary"], C.A5)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _ledger(self):
        p = self.tmp / "ledger.jsonl"
        L = Ledger(p, "RUN-T")
        for i in range(4):
            L.append("ingest", case_id=f"C{i}", result={"i": i})
        L.close()
        return p

    def test_chain_ok(self):
        self.assertEqual(verify_chain(self._ledger()), (True, []))

    def test_edit_breaks_chain(self):
        p = self._ledger()
        lines = p.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace('"i":1', '"i":9')
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        ok, probs = verify_chain(p)
        self.assertFalse(ok)
        self.assertTrue(any("row_sha256 mismatch" in x for x in probs))

    def test_delete_breaks_chain(self):
        p = self._ledger()
        lines = p.read_text(encoding="utf-8").splitlines()
        del lines[2]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertFalse(verify_chain(p)[0])

    def test_ledger_never_reopened(self):
        p = self._ledger()
        with self.assertRaises(FileExistsError):
            Ledger(p, "RUN-T")


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records, _ = load_records(DEFAULT_DATA)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_one(self, recs, verifier=None, cap=C.DEFAULT_CALL_CAP):
        L = Ledger(self.tmp / "l.jsonl", "RUN-T")
        with open(self.tmp / "c.jsonl", "w", encoding="utf-8") as fh:
            eng = Engine(verifier or MockVerifier(recs), L, fh, call_cap=cap)
            outs = [eng.run_case(engine_view(r)) for r in recs]
        L.close()
        return outs, eng

    def test_engine_view_has_no_answer_key(self):
        for r in self.records:
            v = engine_view(r)
            for k in C.ANSWER_KEY_FIELDS:
                self.assertNotIn(k, v)
            for rc in v["receipts"]:
                self.assertNotIn("relation", rc)

    def test_answer_key_mutation_does_not_change_output(self):
        rec = next(r for r in self.records if r["case_id"] == "DEMO-CASE-001")
        poisoned = copy.deepcopy(rec)
        poisoned["verdict"]["state"] = C.REFUTED
        poisoned["engine_expectation"]["expected_state"] = C.REFUTED
        poisoned["history"] = poisoned["history"][:1]
        a, _ = self._run_one([rec])
        shutil.rmtree(self.tmp); self.tmp.mkdir()
        b, _ = self._run_one([poisoned], verifier=MockVerifier([rec]))
        self.assertEqual(a[0]["verdict"], b[0]["verdict"])

    def test_flip_case(self):
        rec = next(r for r in self.records if r["case_id"] == "DEMO-CASE-001")
        out = self._run_one([rec])[0][0]
        self.assertEqual([h["state"] for h in out["history"]], [C.SUBSTANCE, C.UNVERIFIED])
        self.assertEqual(out["alert"]["rule_id"], C.A1)
        self.assertTrue({"R3", "R4", "R5"} <= set(out["verdict"]["evidence_ids"]))

    def test_verifier_failure_is_recorded_not_silent(self):
        class Broken(MockVerifier):
            def classify_evidence(self, req):
                raise VerifierError("simulated 503")
        rec = next(r for r in self.records if r["case_id"] == "DEMO-CASE-002")
        outs, eng = self._run_one([rec], verifier=Broken([rec]))
        self.assertEqual(outs[0]["verdict"]["state"], C.PENDING)
        self.assertEqual(outs[0]["verdict"]["reason_code"], C.SOURCE_UNREACHABLE)
        self.assertGreater(eng.call_errors, 0)

    def test_bad_schema_response_retried_once_then_recorded(self):
        class Garbage(MockVerifier):
            n = 0
            def classify_evidence(self, req):
                Garbage.n += 1
                return {"relation": "maybe"}
        rec = next(r for r in self.records if r["case_id"] == "DEMO-CASE-003")
        outs, eng = self._run_one([rec], verifier=Garbage([rec]))
        self.assertEqual(Garbage.n, 2 * len(rec["receipts"]))
        self.assertNotEqual(outs[0]["verdict"]["state"], C.REFUTED)

    def test_call_cap(self):
        outs, eng = self._run_one([r for r in self.records if r["suite"] == "core_demo"], cap=3)
        self.assertTrue(eng.cap_hit)
        self.assertLessEqual(eng.calls, 3)

    def test_duplicate_flood_detected_across_cases(self):
        rec = next(r for r in self.records if r["case_id"] == "BULK-121")
        copies = []
        for i in range(C.DUPLICATE_FLOOD_THRESHOLD):
            c = copy.deepcopy(rec); c["case_id"] = f"BULK-9{i:02d}"; copies.append(c)
        outs, _ = self._run_one(copies)
        self.assertEqual(outs[-1]["alert"]["rule_id"], C.A5)
        self.assertNotEqual(outs[0]["alert"]["rule_id"], C.A5)


class IngestAndCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_malformed_row_rejected_naming_case_and_field(self):
        d = self.tmp / "data"; d.mkdir()
        shutil.copy(DEFAULT_DATA / "claims.schema.json", d)
        lines = (DEFAULT_DATA / "claims.jsonl").read_text(encoding="utf-8").splitlines()
        bad = json.loads(lines[1]); bad["receipts"][0]["relation"] = "probably"
        (d / "claims.jsonl").write_text("\n".join([lines[0], json.dumps(bad)]) + "\n", encoding="utf-8")
        with self.assertRaises(IngestError) as cm:
            load_records(d)
        self.assertIn("DEMO-CASE-002", str(cm.exception))
        self.assertIn("relation", str(cm.exception))
        self.assertEqual(run_main(["--data", str(d), "--out", str(self.tmp), "--quiet"]), 3)

    def test_empty_suite_fails_loudly(self):
        rc = run_main(["--suite", "empty", "--out", str(self.tmp), "--run-id", "E", "--quiet"])
        self.assertEqual(rc, 1)
        s = json.loads((self.tmp / "E" / "run_summary.json").read_text(encoding="utf-8"))
        self.assertEqual(s["status"], C.RUN_FAILED)
        self.assertFalse(s["live_request_attempted"])
        self.assertFalse(s["muse_spark_called"])
        self.assertEqual(s["run_alerts"][0]["rule_id"], C.A2)

    def test_core_demo_end_to_end_then_replay(self):
        self.assertEqual(run_main(["--suite", "core_demo", "--out", str(self.tmp), "--run-id", "M", "--quiet"]), 0)
        ok, probs, _ = verify_run(self.tmp / "M")
        self.assertTrue(ok, probs)
        g = json.loads((self.tmp / "M" / "grades.json").read_text(encoding="utf-8"))
        for cid in ("DEMO-CASE-001", "DEMO-CASE-002", "DEMO-CASE-003"):
            case = next(c for c in g["cases"] if c["case_id"] == cid)
            self.assertTrue(case["pass"] and case["rule_match"], case)
        self.assertEqual(run_main(["--suite", "core_demo", "--mode", "replay", "--replay-from", str(self.tmp / "M"),
                                   "--out", str(self.tmp), "--run-id", "P", "--quiet"]), 0)
        va = [json.loads(x)["verdict"] for x in (self.tmp / "M" / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()]
        vb = [json.loads(x)["verdict"] for x in (self.tmp / "P" / "verdicts.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(va, vb)

    def test_live_without_adapter_is_setup_error(self):
        self.assertEqual(run_main(["--mode", "live", "--out", str(self.tmp), "--quiet"]), 2)


if __name__ == "__main__":
    unittest.main()
