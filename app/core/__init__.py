"""ClaimWatch app/core: claim ingest -> Muse Spark verify -> deterministic gate -> receipt ledger -> alerts.

ClaimWatch core: ingest, gate, hash-chained receipt ledger, alerts, grading, replay verify.

Layout (one concern per module):
  contracts.py  enums + constants mirrored from demo_data/claims.schema.json (schema 1.0.0)
  ingest.py     load JSONL, validate every row against the schema, strip the answer key
  verifier.py   the M1/M2 adapter contract + mock (fixture oracle) and replay adapters
  gate.py       pure verdict functions (no I/O, no network)
  alerts.py     alert rules A1..A5
  ledger.py     append-only, sha256-chained receipt ledger + chain/replay verification
  engine.py     the per-case pipeline that wires the above together
  grade.py      grader: compares engine output to engine_expectation (the ONLY reader of the answer key)
  run.py        CLI harness: python -m app.core.run --suite core_demo --mode mock
  selftest.py   unit tests: python -m unittest app.core.selftest
"""

__version__ = "0.1.0"
