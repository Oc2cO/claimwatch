"""Offline tests for app/muse_verify (no network). Run: python -m unittest app.muse_verify.selftest
with PYTHONPATH = the repo root."""
import json
import unittest

from app.core.verifier import VerifierError, load_live_verifier
from app.muse_verify import MuseVerifier, get_verifier

M1_REQ = {"case_id": "T-1", "claim_text": "We cut inference cost 40% in Q3 by caching.", "source_type": "report"}
M2_REQ = {"case_id": "T-1", "assertion": {"assertion_id": "A1", "text": "cost cut 40%", "kind": "quantitative",
                                          "checkable": True},
          "receipt": {"receipt_id": "R1", "kind": "note", "ref": "x", "note": "no invoices attached",
                      "supports_assertion": "A1", "artifact_sha256": "IGNORED"}}


def fake(responses):
    seen = []

    def t(name, system, user, schema):
        seen.append((name, system, user, schema))
        return responses[name]
    t.seen = seen
    return t


class MuseVerifyTests(unittest.TestCase):
    def test_m1_valid(self):
        t = fake({"M1": json.dumps({"assertions": [
            {"assertion_id": "A1", "text": "cost cut 40%", "kind": "quantitative", "checkable": True},
            {"assertion_id": "A2", "text": "caching caused it", "kind": "causal", "checkable": True}]})})
        v = get_verifier(transport=t, model="test-model")
        self.assertEqual(v.mode, "live")
        self.assertEqual(v.model, "test-model")
        self.assertEqual(len(v.decompose_claim(M1_REQ)["assertions"]), 2)
        self.assertEqual(json.loads(t.seen[0][2])["claim_text"], M1_REQ["claim_text"])

    def test_m2_fenced_json_ok_and_receipt_view_limited(self):
        t = fake({"M2": '```json\n{"relation":"insufficient","rationale":"no invoices","quoted_span":"no invoices"}\n```'})
        out = get_verifier(transport=t, model="m").classify_evidence(M2_REQ)
        self.assertEqual(out["relation"], "insufficient")
        self.assertNotIn("supports_assertion", json.loads(t.seen[0][2])["receipt"])  # model never sees the key hint

    def test_m2_bad_relation_raises(self):
        t = fake({"M2": json.dumps({"relation": "maybe", "rationale": "x", "quoted_span": ""})})
        with self.assertRaises(VerifierError):
            get_verifier(transport=t, model="m").classify_evidence(M2_REQ)

    def test_m1_bad_kind_raises(self):
        t = fake({"M1": json.dumps({"assertions": [{"assertion_id": "A1", "text": "x", "kind": "vibe",
                                                    "checkable": True}]})})
        with self.assertRaises(VerifierError):
            get_verifier(transport=t, model="m").decompose_claim(M1_REQ)

    def test_non_json_raises(self):
        with self.assertRaises(VerifierError):
            get_verifier(transport=fake({"M1": "Sure! Here are the assertions..."}), model="m").decompose_claim(M1_REQ)

    def test_m3_two_sentence_cap(self):
        v = get_verifier(transport=fake({"M3": json.dumps({"summary": "One. Two. Three."})}), model="m")
        with self.assertRaises(VerifierError):
            v.explain_verdict({"case_id": "T-1", "state": "UNVERIFIED", "receipts": []})
        v = get_verifier(transport=fake({"M3": json.dumps({"summary": "No invoice backs A1. It stays unverified."})}),
                         model="m")
        self.assertIn("summary", v.explain_verdict({"case_id": "T-1", "state": "UNVERIFIED", "receipts": []}))

    def test_missing_model_raises(self):
        with self.assertRaises(VerifierError):
            MuseVerifier(fake({}), "")

    def test_core_loader_without_transport_is_setup_error_not_import_error(self):
        with self.assertRaises(VerifierError) as cm:
            load_live_verifier()
        self.assertIn("live mode requires MODEL_API_KEY", str(cm.exception))
        self.assertNotIn("not importable: No module named 'app.muse_verify'", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
