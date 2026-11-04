"""app/api: the live transport seam for the Muse Spark verify step.

What app/core and app/muse_verify expect from this module (contract is app/muse_verify's docstring):

    get_transport() -> (transport_callable, model_id)

    transport_callable(call_name: str, system: str, user: str, schema: dict) -> str    raw model text

Design rules this module holds to, and why:

1. HONEST LABELING OR NOTHING. Every transport returned here carries `.mode` ("live" | "mock") and `.network`
   (bool). app/muse_verify derives its `.mode` from the transport. The run summary separates a live request
   attempt from a successful HTTP-200 response; the label alone is not proof of a provider call.
   A transport that does not talk to Meta must report mode="mock". The run summary distinguishes a request
   attempt from an observed HTTP-200 response, so the mode label alone never claims successful model use.

2. FIXED AND BOUNDED LIVE REQUEST. The documented HTTPS endpoint and model are constants. Only MODEL_API_KEY
   and optional CLAIMWATCH_MAX_REQUESTS are read from the environment. The request budget defaults to 1 and is
   capped at 4, with 512 completion tokens, minimal reasoning, bounded prompt/body/response sizes, short timeout,
   no transport retries, and redirects disabled. No socket opens at import or construction. This is not a billing cap.

3. THE MOCK HERE IS A TEST SEAM, NOT THE DEMO. The demo pipeline runs `--mode mock`, which uses
   app.core.verifier.MockVerifier (a fixture oracle reproducing the recorded relations). MockTransport in this
   module is synthetic and deliberately CANNOT manufacture support: it answers "insufficient" to every M2 call,
   so a run driven by it can never produce a SUBSTANCE verdict it did not earn. It exists so the live seam is
   importable and testable offline, and so a *labeled* mock run is available for plumbing checks.

Environment (all read at call time, never at import):

    CLAIMWATCH_TRANSPORT_MODE   "live" (default) | "mock"
    MODEL_API_KEY               credential for the fixed documented endpoint
    CLAIMWATCH_MAX_REQUESTS     optional integer 1-4; default 1
"""
import json
import os
import urllib.error
import urllib.request

from app.core.verifier import (NonRetryableTransportError, RetryableResponseError, VerifierError,
                               validate_m1, validate_m2)

__all__ = [
    "get_transport", "transport_meta", "MockTransport", "HttpTransport", "VerifierError",
    "NonRetryableTransportError", "RetryableResponseError",
    "MOCK_MODEL_ID", "MODE_LIVE", "MODE_MOCK",
    "ENV_MODE", "ENV_KEY", "ENV_MAX_REQUESTS", "REQUIRED_ENV", "LIVE_ENDPOINT", "LIVE_MODEL_ID",
    "MAX_COMPLETION_TOKENS", "MAX_INPUT_BYTES", "MAX_REQUEST_BYTES", "MAX_RESPONSE_BYTES",
    "MAX_REQUESTS", "DEFAULT_MAX_REQUESTS",
]

MODE_LIVE = "live"
MODE_MOCK = "mock"

ENV_MODE = "CLAIMWATCH_TRANSPORT_MODE"
ENV_KEY = "MODEL_API_KEY"
ENV_MAX_REQUESTS = "CLAIMWATCH_MAX_REQUESTS"
LIVE_ENDPOINT = "https://api.meta.ai/v1/chat/completions"
LIVE_MODEL_ID = "muse-spark-1.3"
REQUIRED_ENV = (ENV_KEY,)
MAX_COMPLETION_TOKENS = 512
MAX_INPUT_BYTES = 8192
MAX_REQUEST_BYTES = 8192
MAX_RESPONSE_BYTES = 32768
MAX_REQUESTS = 4
DEFAULT_MAX_REQUESTS = 1
MAX_TIMEOUT_SECONDS = 30

MOCK_MODEL_ID = "mock://app/api-mock-transport"

# The mock's own words, kept in one place so the rationale stays under the 300-char M2 limit.
MOCK_RATIONALE = "mock transport: no model was consulted, so no evidence was read. Absence of evidence is not support."
MOCK_M1_TEXT_MAX = 200


class MockTransport:
    """Synthetic, offline, honestly labeled. Not Muse Spark. Cannot produce a 'supports' relation."""

    mode = MODE_MOCK
    network = False

    def __init__(self, model_id=MOCK_MODEL_ID):
        self.model = model_id
        self.calls = 0

    def __call__(self, call_name, system, user, schema):
        self.calls += 1
        try:
            payload = json.loads(user)
        except (TypeError, ValueError):
            payload = {}
        if call_name == "M1":
            claim = payload.get("claim_text") or ""
            if not isinstance(claim, str):
                claim = str(claim)
            text = claim.strip()[:MOCK_M1_TEXT_MAX] or "unparseable input"
            return json.dumps({"assertions": [
                {"assertion_id": "A1", "text": text, "kind": "existence", "checkable": True}]})
        if call_name == "M2":
            return json.dumps({"relation": "insufficient", "rationale": MOCK_RATIONALE, "quoted_span": ""})
        if call_name == "M3":
            return json.dumps({"summary": "Mock transport: no model was consulted. This text is not evidence."})
        raise VerifierError(f"app/api mock transport: unknown call_name {call_name!r}")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HttpTransport:
    """Bounded Meta Model API transport; one synthetic full live case is archived with this release."""

    mode = MODE_LIVE
    network = True
    base_url = LIVE_ENDPOINT
    model = LIVE_MODEL_ID

    def __init__(self, api_key, timeout_s=MAX_TIMEOUT_SECONDS, max_requests=DEFAULT_MAX_REQUESTS):
        if not isinstance(api_key, str) or not api_key.strip():
            raise VerifierError("app/api: MODEL_API_KEY is required")
        if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or not (1 <= timeout_s <= MAX_TIMEOUT_SECONDS):
            raise VerifierError("app/api: timeout is outside the fixed safe range")
        if not isinstance(max_requests, int) or isinstance(max_requests, bool) or not (1 <= max_requests <= MAX_REQUESTS):
            raise VerifierError("app/api: request budget must be an integer from 1 to 4")
        self._key = api_key
        self._max_requests = max_requests
        self.timeout_s = timeout_s
        self.calls = 0
        self.successful_http_responses = 0
        self._response_metadata = None

    @property
    def max_requests(self):
        return self._max_requests

    @property
    def max_completion_tokens(self):
        return MAX_COMPLETION_TOKENS

    @property
    def reasoning_effort(self):
        return "minimal"

    @property
    def response_metadata(self):
        if self._response_metadata is None:
            return None
        usage = self._response_metadata["usage"]
        return dict(self._response_metadata, usage=dict(usage) if usage is not None else None)

    def __call__(self, call_name, system, user, schema):
        if self.calls >= self.max_requests:
            raise NonRetryableTransportError("live transport request budget is exhausted")
        if not isinstance(system, str) or not isinstance(user, str):
            raise NonRetryableTransportError("live transport requires text prompts")
        try:
            input_bytes = len(system.encode("utf-8")) + len(user.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise NonRetryableTransportError("live transport prompt is not valid UTF-8") from exc
        if input_bytes > MAX_INPUT_BYTES:
            raise NonRetryableTransportError("live transport prompt exceeds the fixed input limit")
        body = json.dumps({
            "model": LIVE_MODEL_ID,
            "temperature": 0,
            "n": 1,
            "reasoning_effort": self.reasoning_effort,
            "max_completion_tokens": self.max_completion_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }).encode("utf-8")
        if len(body) > MAX_REQUEST_BYTES:
            raise NonRetryableTransportError("live transport request exceeds the fixed body limit")
        req = urllib.request.Request(LIVE_ENDPOINT, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._key}",
        })
        self.calls += 1
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler())
            with opener.open(req, timeout=self.timeout_s) as resp:
                status = resp.getcode()
                self._response_metadata = {"http_status": status, "model": None, "usage": None}
                if status != 200:
                    raise NonRetryableTransportError(f"{call_name}: API returned HTTP {status}")
                self.successful_http_responses += 1
                raw_bytes = resp.read(MAX_RESPONSE_BYTES + 1)
                if len(raw_bytes) > MAX_RESPONSE_BYTES:
                    raise NonRetryableTransportError(f"{call_name}: API response exceeds the fixed size limit")
                raw = raw_bytes.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise NonRetryableTransportError(f"{call_name}: API returned HTTP {exc.code}") from exc
        except NonRetryableTransportError:
            raise
        except Exception as exc:
            raise NonRetryableTransportError(f"{call_name}: transport failure ({type(exc).__name__})") from exc
        try:
            doc = json.loads(raw)
        except ValueError as exc:
            raise RetryableResponseError(f"{call_name}: endpoint returned non-JSON") from exc
        if isinstance(doc, dict) and self._response_metadata is not None:
            model = doc.get("model")
            usage = doc.get("usage")
            safe_usage = None
            if isinstance(usage, dict):
                safe_usage = {k: v for k, v in usage.items()
                              if k in ("prompt_tokens", "completion_tokens", "total_tokens")
                              and type(v) is int and 0 <= v <= 1000000}
            self._response_metadata["model"] = model[:100] if isinstance(model, str) else None
            self._response_metadata["usage"] = safe_usage
        text = _extract_text(doc)
        if text is None:
            raise RetryableResponseError(f"{call_name}: endpoint returned no text field")
        return text


def _extract_text(doc):
    """Best-effort text extraction from an unverified response shape. Returns None if nothing matches."""
    if not isinstance(doc, dict):
        return None
    choices = doc.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        msg = choices[0].get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), str):
            return msg["content"]
        if isinstance(choices[0].get("text"), str):
            return choices[0]["text"]
    for key in ("output_text", "text", "content", "response"):
        if isinstance(doc.get(key), str):
            return doc[key]
    return None


def get_transport(mode=None, model=None, env=None, max_requests=None):
    """Return a labeled transport; live mode reads MODEL_API_KEY and a capped request budget (default 1)."""
    env = os.environ if env is None else env
    selected = (mode or env.get(ENV_MODE) or MODE_LIVE)
    selected = str(selected).strip().lower()

    if selected == MODE_MOCK:
        transport = MockTransport(model or MOCK_MODEL_ID)
        return transport, transport.model

    if selected != MODE_LIVE:
        raise VerifierError(
            f"app/api: unknown transport mode {selected!r}; expected {MODE_LIVE!r} or {MODE_MOCK!r}")

    api_key = env.get(ENV_KEY)
    if not api_key:
        raise VerifierError(f"app/api: live mode requires {ENV_KEY}; endpoint/model are fixed in this build")
    if model is not None and model != LIVE_MODEL_ID:
        raise VerifierError("app/api: only the documented model is allowed")
    budget = max_requests if max_requests is not None else env.get(ENV_MAX_REQUESTS, DEFAULT_MAX_REQUESTS)
    if isinstance(budget, str):
        try:
            budget = int(budget)
        except ValueError as exc:
            raise VerifierError("app/api: request budget must be an integer from 1 to 4") from exc
    transport = HttpTransport(api_key, max_requests=budget)
    return transport, transport.model


def transport_meta(transport):
    """Honest label for a transport, for any caller that wants provenance without guessing."""
    return {
        "mode": getattr(transport, "mode", MODE_LIVE),
        "network": bool(getattr(transport, "network", False)),
        "model": getattr(transport, "model", None),
        "class": type(transport).__name__,
    }


# ---- offline self-check: the seam is honest and its mock cannot manufacture support -------------------
if __name__ == "__main__":
    t, mid = get_transport(mode=MODE_MOCK)
    checks = []
    checks.append(("mock mode label", t.mode == MODE_MOCK))
    checks.append(("mock network flag", t.network is False))
    m1 = json.loads(t("M1", "", json.dumps({"claim_text": "sales rose 40% qoq"}), {}))
    checks.append(("M1 validates", validate_m1(m1) is None))
    m2 = json.loads(t("M2", "", json.dumps({"assertion": {}, "receipt": {}}), {}))
    checks.append(("M2 validates", validate_m2(m2) is None))
    checks.append(("M2 never supports", m2["relation"] == "insufficient"))
    try:
        get_transport(env={})
        checks.append(("live refuses without creds", False))
    except VerifierError as exc:
        checks.append(("live refuses without creds", all(n in str(exc) for n in REQUIRED_ENV)))
    for label, good in checks:
        print(("PASS " if good else "FAIL ") + label)
    raise SystemExit(0 if all(g for _, g in checks) else 1)
