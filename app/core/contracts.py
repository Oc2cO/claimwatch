"""Canonical enums. Mirrors demo_data/claims.schema.json v1.0.0. No second enum is introduced."""

SCHEMA_VERSION = "1.0.0"

# Verdict states (schema enum). Display labels are UI-only.
PENDING = "PENDING"
SUBSTANCE = "SUBSTANCE"
UNVERIFIED = "UNVERIFIED"
REFUTED = "REFUTED"
STATES = (PENDING, SUBSTANCE, UNVERIFIED, REFUTED)
DISPLAY_LABEL = {
    SUBSTANCE: "Verified (substance)",
    UNVERIFIED: "Unverified",
    REFUTED: "Refuted",
    PENDING: "Pending",
}

# Reason codes (schema enum).
EVIDENCE_SUFFICIENT = "EVIDENCE_SUFFICIENT"
EVIDENCE_MISSING = "EVIDENCE_MISSING"
EVIDENCE_CONTRADICTS = "EVIDENCE_CONTRADICTS"
CLAIM_UNCHECKABLE = "CLAIM_UNCHECKABLE"
DUPLICATE_CLAIM = "DUPLICATE_CLAIM"
SOURCE_UNREACHABLE = "SOURCE_UNREACHABLE"
PRICING_STALE = "PRICING_STALE"
SCOPE_MISMATCH = "SCOPE_MISMATCH"
TEMPORAL_MISMATCH = "TEMPORAL_MISMATCH"
RUN_EMPTY = "RUN_EMPTY"
RUN_PARTIAL = "RUN_PARTIAL"
REASON_CODES = (
    EVIDENCE_SUFFICIENT, EVIDENCE_MISSING, EVIDENCE_CONTRADICTS, CLAIM_UNCHECKABLE,
    DUPLICATE_CLAIM, SOURCE_UNREACHABLE, PRICING_STALE, SCOPE_MISMATCH,
    TEMPORAL_MISMATCH, RUN_EMPTY, RUN_PARTIAL,
)

# Evidence relations (M2 output).
SUPPORTS = "supports"
CONTRADICTS = "contradicts"
INSUFFICIENT = "insufficient"
RELATIONS = (SUPPORTS, CONTRADICTS, INSUFFICIENT)

ASSERTION_KINDS = ("quantitative", "causal", "comparative", "temporal", "existence")

# Alert rules (schema enum) and the priority used to pick the one "primary" rule per case.
A1 = "A1_VERDICT_DOWNGRADE"
A2 = "A2_EMPTY_RUN"
A3 = "A3_UNSUPPORTED_ASSERTION"
A4 = "A4_REFUTED"
A5 = "A5_DUPLICATE_FLOOD"
NO_ALERT = "none"
ALERT_PRIORITY = (A2, A1, A5, A4, A3)
ALERT_SEVERITY = {A2: "critical", A1: "high", A4: "high", A5: "medium", A3: "medium"}

# Run status.
RUN_SUCCESS = "SUCCESS"
RUN_FAILED = "FAILED"
RUN_STATUS_PARTIAL = "PARTIAL"

# Gate passes. The naive pass is the failure the product exists to catch; it is recorded, never trusted.
PASS_NAIVE = "naive_baseline"
PASS_RECEIPT = "receipt_gate"

# Fields the ENGINE may read. Everything else in a record is the grader's answer key
# (demo_data/README s5: "Do not read verdict or engine_expectation inside the engine").
ENGINE_RECORD_FIELDS = ("case_id", "schema_version", "suite", "run_id", "title", "source", "claims", "receipts")
ENGINE_RECEIPT_FIELDS = ("receipt_id", "kind", "ref", "retrieved_at", "supports_assertion", "artifact_sha256", "note")
ANSWER_KEY_FIELDS = ("verdict", "history", "alert", "engine_expectation", "notes", "evidence_checklist")

DEFAULT_CALL_CAP = 200          # budget guard
DUPLICATE_FLOOD_THRESHOLD = 3   # same normalized claim text seen >= N times in one run -> A5
