# Archived live synthetic proof

This directory contains the explicit public-safe proof bundle for one ClaimWatch full-core live case. The fixture is fictional and includes no account, customer, key, payment, or private vault data. Two real HTTP 200 responses from `muse-spark-1.3` passed M1/M2, resulting in SUBSTANCE citing R1. The ledger/replay verifier and sanitizer passed.

`proof_summary.json` records the run and file hashes. `input/` holds the fictional claim schema and record; `run/` holds the actual run summary, calls, ledger, grade, verdict, and alert files. Verify from the project root with:

```powershell
python -B -m app.core.verify evidence/live_synthetic_20261104/run
python -B -m app.core.sanitize_run evidence/live_synthetic_20261104/run
```

The 70-case fixture benchmark remains MOCK / REPLAY (68/70, with two disclosed misses). The live evidence is one successful case only. Two separate standalone M1 requests are also disclosed in `proof_summary.json`: the first returned HTTP 200 but failed schema validation; the second diagnostic passed. This does not establish why the first failed or claim broader model quality.

The original private coordination receipt is not included. Its content contains run coordination and local provenance outside this public evidence scope; the SHA-256 is retained only as a source integrity reference.