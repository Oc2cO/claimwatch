# ClaimWatch

**AI work that shows its evidence, and says so when it has none.**
Meta Global AI Hackathon 2026, Indie track.

Most agent pipelines fail silently. An empty run reports "success", and a plausible claim is kept with no source behind it. ClaimWatch fails out loud. Every claim it keeps cites a receipt. Every gap is labeled. Every decision lands in a hash-chained ledger that anyone can re-verify.

> **Honesty label:** the four example runs in `app/core/runs/` are MOCK / REPLAY (`model: mock-fixture-oracle`); each has `"muse_spark_called": false`. A separate archived live proof covers one fictional synthetic case, with two successful HTTP 200 calls. It is not a broad quality or reliability result.

## Demo video

`video/claimwatch_live_evidence_v1.mp4` is a 70-second silent, captioned recording of archived evidence from one fictional synthetic live case. It shows two successful Muse Spark HTTP 200 responses, the saved dashboard, the separate standalone M1 schema failure and later diagnostic pass, and the mock/replay benchmark clearly labeled separately. No API call is happening during the video. Its sidecar captions and SHA-256 values are listed in `video/SHA256SUMS.txt`. This single-case live run is not broad accuracy or reliability evidence; the separate 70-case MOCK / REPLAY fixture benchmark remains 68/70 with misses `BULK-108` and `BULK-205`.

## What it does

```
claims -> ingest (schema 1.0.0) -> verifier (Muse Spark | mock | replay)
       -> deterministic gate -> verdict + alert -> append-only sha256-chained ledger
       -> grading vs fixture -> replay verify -> static dashboard
```

Verdicts:

| Verdict | Meaning |
|---|---|
| **Verified (substance)** | every checkable assertion has a supporting receipt |
| **Unverified** | something lacks evidence, but nothing contradicts it. Absence of evidence never refutes. |
| **Refuted** | a receipt contradicts the claim |
| **Pending** | nothing could be checked |

Alerts: `A1_VERDICT_DOWNGRADE`, `A2_EMPTY_RUN`, `A3_UNSUPPORTED_ASSERTION`, `A4_REFUTED` and `A5_DUPLICATE_FLOOD`.

## Run it

Python 3 with the `jsonschema` package. Run from the repo root.

```bash
pip install jsonschema

# the failure that matters: an empty run is FAILED (exit 1), never "success"
python -B -m app.core.run --suite empty --out runs --run-id my-empty

# the 5 hero cases (flip, positive control, refuted, empty-run case, duplicate flood)
python -B -m app.core.run --suite core_demo --mode mock --out runs --run-id my-core

# all 70 cases (5 core + 57 bulk + 8 adversarial)
python -B -m app.core.run --suite all --mode mock --out runs --run-id my-all

# re-verify a run: hash chain + gate replay; edit any ledger row and this fails at that line
python -B -m app.core.verify runs/my-core

# sanitization sweep of a run (emails, phones, keys, user paths, hosts, handles)
python -B -m app.core.sanitize_run runs/my-core

# tests
python -B -m unittest app.core.selftest app.muse_verify.selftest app.api.selftest app.ui.test_ui
python -B demo_data/validate_demo_data.py
```

Dashboard: open `app/ui/dist/dashboard.html` in a browser. It works offline from `file://`, and it re-checks every ledger row hash in the browser with WebCrypto.

## Results on the included runs (`app/core/runs/`)

| Run | Result |
|---|---|
| `RUN-CORE-003-empty` | `STATUS FAILED`, alert `A2_EMPTY_RUN`, exit 1 |
| `RUN-CORE-001-mock` | core_demo graded 5/5. Status prints `PARTIAL` because DEMO-CASE-004 *is* the empty-run case, failed on purpose. |
| `RUN-CORE-002-replay` | the same 5 cases replayed from recorded calls, identical verdicts |
| `RUN-CORE-004-all-mock` | 70 cases, **68/70**. Misses, disclosed: `BULK-108`, `BULK-205`. |

Each run verifies with `chain+replay PASS`. Changing one ledger row (for example, `REFUTED` to `SUBSTANCE`) makes verify fail with `row_sha256 mismatch` at that line.

## Live mode (Muse Spark)

`--mode live` uses the documented `https://api.meta.ai/v1/chat/completions` endpoint and `muse-spark-1.3` model, with `MODEL_API_KEY` supplied by the operator. The transport uses `n=1`, `reasoning_effort=minimal`, and `max_completion_tokens=512`, bounds prompt/request/response sizes, makes no transport retries, and refuses redirects. Its shared request budget defaults to 1 and is capped at 4 (`CLAIMWATCH_MAX_REQUESTS`); a one-record multi-step run must explicitly choose a budget no greater than 4. This is not a billing cap. The run summary separates `live_request_attempted` from `muse_spark_called`, which is true only after an HTTP 200 response. One full live run on a fictional synthetic case is archived under `evidence/live_synthetic_20261104/`: two HTTP 200 responses produced a `SUBSTANCE` verdict citing R1, the grader passed 1/1, and ledger/replay and sanitization checks passed. This demonstrates one successful path, not broad accuracy or reliability. Two separate standalone M1 requests are also disclosed there: attempt 001 returned HTTP 200 but failed schema validation; diagnostic 002 passed schema. The first failure cause is unknown; reaching the token cap does not prove truncation. The run and its fixtures are synthetic; no private source data is included.

## Data

All demo data is synthetic and sanitized (`demo_data/`, schema `claims.schema.json` 1.0.0). It holds no real people, accounts, credentials or customer data. `validate_demo_data.py` enforces this together with schema and consistency checks.

## Verification scope

The included `app/core/runs/` examples are local MOCK / REPLAY fixtures; the 70-case fixture benchmark grades 68/70 and discloses two misses (`BULK-108`, `BULK-205`). The separate one-case live archive is not part of that benchmark. Verify its saved artifacts from the project root with `python -B -m app.core.verify evidence/live_synthetic_20261104/run` and `python -B -m app.core.sanitize_run evidence/live_synthetic_20261104/run`. These checks validate the archived hash chain, replay, and sanitization; they do not measure model quality beyond that single synthetic case.
