"""Offline tests for app/api. Run from the project root:  python -m unittest app.api.selftest

No test here opens a socket. The live request shape is exercised through a fake opener; credentials are never
read by tests. The point of these tests is the HONESTY and request-boundary properties:
a transport that does not talk to Meta must never let a run claim it did.
"""
import json
import unittest
from unittest.mock import patch

from app.api import (ENV_KEY, ENV_MAX_REQUESTS, ENV_MODE, LIVE_ENDPOINT, LIVE_MODEL_ID, MAX_COMPLETION_TOKENS,
                     MAX_INPUT_BYTES, MAX_REQUESTS, DEFAULT_MAX_REQUESTS, MOCK_MODEL_ID,
                     MODE_LIVE, MODE_MOCK, REQUIRED_ENV, HttpTransport, MockTransport,
                     _NoRedirectHandler, get_transport, transport_meta)
from app.core.verifier import VerifierError, validate_m1, validate_m2
from app.muse_verify import get_verifier, validate_m3


class LiveRefusesWhenUnconfigured(unittest.TestCase):
    def test_empty_env_names_every_missing_variable(self):
        with self.assertRaises(VerifierError) as cm:
            get_transport(env={})
        msg = str(cm.exception)
        for name in REQUIRED_ENV:
            self.assertIn(name, msg)

    def test_non_documented_model_refuses(self):
        with self.assertRaises(VerifierError):
            get_transport(model="other-model", env={ENV_KEY: "synthetic-test-key"})

    def test_explicit_request_budget_is_bounded(self):
        t, _ = get_transport(env={ENV_KEY: "synthetic-test-key", ENV_MAX_REQUESTS: "3"})
        self.assertEqual(t.max_requests, 3)
        with self.assertRaises(VerifierError):
            get_transport(max_requests=MAX_REQUESTS + 1, env={ENV_KEY: "synthetic-test-key"})

    def test_missing_key_and_endpoint_override_do_not_open_network(self):
        with patch("app.api.urllib.request.build_opener") as build:
            with self.assertRaises(VerifierError):
                get_transport(env={})
            with self.assertRaises(VerifierError):
                get_transport(model="unapproved", env={ENV_KEY: "synthetic-test-key"})
            t, _ = get_transport(env={ENV_KEY: "synthetic-test-key",
                                      "CLAIMWATCH_API_ENDPOINT": "https://other.invalid/"})
            self.assertEqual(t.base_url, LIVE_ENDPOINT)
            build.assert_not_called()

    def test_unknown_mode_refuses(self):
        with self.assertRaises(VerifierError):
            get_transport(mode="production", env={})


class LabeledMock(unittest.TestCase):
    def test_mode_selected_by_env(self):
        self.assertEqual(ENV_MODE, "CLAIMWATCH_TRANSPORT_MODE")
        t, mid = get_transport(env={ENV_MODE: MODE_MOCK})
        self.assertEqual(t.mode, MODE_MOCK)
        self.assertIs(t.network, False)
        self.assertEqual(mid, MOCK_MODEL_ID)

    def test_mock_outputs_pass_core_validators(self):
        t, _ = get_transport(mode=MODE_MOCK, env={})
        m1 = json.loads(t("M1", "", json.dumps({"claim_text": "revenue grew 40% qoq"}), {}))
        self.assertIsNone(validate_m1(m1))
        m2 = json.loads(t("M2", "", json.dumps({"assertion": {}, "receipt": {}}), {}))
        self.assertIsNone(validate_m2(m2))
        m3 = json.loads(t("M3", "", json.dumps({"state": "UNVERIFIED", "receipts": []}), {}))
        self.assertIsNone(validate_m3(m3))

    def test_mock_cannot_manufacture_support(self):
        t, _ = get_transport(mode=MODE_MOCK, env={})
        for _ in range(5):
            rel = json.loads(t("M2", "", json.dumps({"assertion": {}, "receipt": {}}), {}))["relation"]
            self.assertEqual(rel, "insufficient")

    def test_mock_is_not_reachable_by_accident(self):
        # default is live, so an unconfigured box can never silently fall through to the synthetic mock
        with self.assertRaises(VerifierError):
            get_transport(env={})

    def test_unknown_call_name_refuses(self):
        t, _ = get_transport(mode=MODE_MOCK, env={})
        with self.assertRaises(VerifierError):
            t("M9", "", "{}", {})


class HonestyFactory(unittest.TestCase):
    def test_verifier_mode_follows_transport_not_class(self):
        t, _ = get_transport(mode=MODE_MOCK, env={})
        v = get_verifier(transport=t, model=t.model)
        self.assertEqual(v.mode, MODE_MOCK)
        # A mock has no successful HTTP response and must never count as a provider call.
        self.assertFalse(v.mode == MODE_LIVE)

    def test_live_transport_builds_without_calling(self):
        env = {ENV_KEY: "synthetic-test-key"}
        t, mid = get_transport(env=env)
        self.assertIsInstance(t, HttpTransport)
        self.assertEqual(t.mode, MODE_LIVE)
        self.assertIs(t.network, True)
        self.assertEqual(mid, LIVE_MODEL_ID)
        self.assertEqual(t.base_url, LIVE_ENDPOINT)
        self.assertEqual(t.max_completion_tokens, MAX_COMPLETION_TOKENS)
        self.assertEqual(t.reasoning_effort, "minimal")
        self.assertEqual(t.max_requests, DEFAULT_MAX_REQUESTS)
        v = get_verifier(transport=t, model=mid)
        self.assertEqual(v.mode, MODE_LIVE)
        self.assertEqual(t.calls, 0)          # construction opens no socket

    def test_bounded_payload_and_response_metadata_use_fake_http_only(self):
        payload = json.dumps({"choices": [{"message": {"content": "{\"ok\":true}"}}],
                              "model": LIVE_MODEL_ID,
                              "usage": {"prompt_tokens": 11, "completion_tokens": 7,
                                        "total_tokens": 18, "unexpected": "ignored"}}).encode()

        class FakeResponse:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def getcode(self): return self.status
            def read(self, limit): return payload[:limit]

        class FakeOpener:
            def __init__(self): self.requests = []
            def open(self, request, timeout):
                self.requests.append((request, timeout))
                return FakeResponse()

        fake = FakeOpener()
        with patch("app.api.urllib.request.build_opener", return_value=fake) as build:
            t = HttpTransport("synthetic-test-key", max_requests=2)
            v = get_verifier(transport=t, model=t.model)
            self.assertEqual(v.successful_http_responses, 0)
            result = t("M1", "bounded system", "bounded user", {})
            self.assertEqual(v.successful_http_responses, 1)
            t("M1", "bounded system", "bounded user", {})
            with self.assertRaises(VerifierError):
                t("M1", "bounded system", "bounded user", {})
        req, timeout = fake.requests[0]
        body = json.loads(req.data)
        self.assertEqual(result, "{\"ok\":true}")
        self.assertEqual(req.full_url, LIVE_ENDPOINT)
        self.assertEqual(req.get_header("Authorization"), "Bearer synthetic-test-key")
        self.assertEqual(body["model"], LIVE_MODEL_ID)
        self.assertEqual(body["n"], 1)
        self.assertEqual(body["max_completion_tokens"], MAX_COMPLETION_TOKENS)
        self.assertEqual(body["reasoning_effort"], "minimal")
        self.assertEqual(timeout, 30)
        self.assertIsInstance(build.call_args.args[0], _NoRedirectHandler)
        self.assertEqual(t.calls, 2)
        self.assertEqual(t.successful_http_responses, 2)
        self.assertEqual(t.response_metadata["http_status"], 200)
        self.assertEqual(t.response_metadata["usage"], {"prompt_tokens": 11, "completion_tokens": 7,
                                                          "total_tokens": 18})

    def test_redirects_are_disabled_and_instance_is_single_request(self):
        handler = _NoRedirectHandler()
        request = __import__("urllib.request", fromlist=["Request"]).Request(LIVE_ENDPOINT)
        self.assertIsNone(handler.redirect_request(request, None, 302, "Found", {}, "https://other.invalid/"))

        class FakeResponse:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def getcode(self): return 200
            def read(self, limit): return b'{"choices":[{"text":"ok"}]}'

        class FakeOpener:
            def __init__(self): self.count = 0
            def open(self, request, timeout): self.count += 1; return FakeResponse()

        fake = FakeOpener()
        with patch("app.api.urllib.request.build_opener", return_value=fake):
            t = HttpTransport("synthetic-test-key", max_requests=2)
            t("M1", "s", "u", {})
            t("M2", "s", "u", {})
            with self.assertRaises(VerifierError):
                t("M3", "s", "u", {})
        self.assertEqual(fake.count, 2)

    def test_prompt_and_response_limits_are_enforced(self):
        class FakeOpener:
            def open(self, request, timeout):
                class FakeResponse:
                    def __enter__(self): return self
                    def __exit__(self, *args): return False
                    def getcode(self): return 200
                    def read(self, limit): return b"x" * limit
                return FakeResponse()

        with patch("app.api.urllib.request.build_opener", return_value=FakeOpener()) as build:
            prompt_limited = HttpTransport("synthetic-test-key")
            with self.assertRaises(VerifierError):
                prompt_limited("M1", "x" * MAX_INPUT_BYTES, "x", {})
            request_limited = HttpTransport("synthetic-test-key")
            with self.assertRaises(VerifierError):
                request_limited("M1", "x" * (MAX_INPUT_BYTES - 1), "", {})
            build.assert_not_called()
            response_limited = HttpTransport("synthetic-test-key")
            with self.assertRaises(VerifierError):
                response_limited("M1", "s", "u", {})
            self.assertEqual(response_limited.successful_http_responses, 1)

    def test_transport_meta_is_factual(self):
        t, _ = get_transport(mode=MODE_MOCK, env={})
        meta = transport_meta(t)
        self.assertEqual(meta["mode"], MODE_MOCK)
        self.assertFalse(meta["network"])
        self.assertEqual(meta["class"], "MockTransport")

    def test_mockverifier_fixture_oracle_is_still_mock(self):
        # the demo path must stay on the fixture oracle, not on this synthetic transport
        from app.core.verifier import MockVerifier
        self.assertEqual(MockVerifier.mode, "mock")
        self.assertNotIsInstance(MockVerifier([]), MockTransport)


if __name__ == "__main__":
    unittest.main(verbosity=2)
