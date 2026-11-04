"""F6 alert engine. Rules from the schema enum; evaluated on engine output only.

A1_VERDICT_DOWNGRADE      history goes SUBSTANCE -> UNVERIFIED/REFUTED
A2_EMPTY_RUN              case (or run) processed zero assertions (I5)
A3_UNSUPPORTED_ASSERTION  final UNVERIFIED/PENDING with >= 1 checkable assertion lacking a supports receipt
A4_REFUTED                final REFUTED
A5_DUPLICATE_FLOOD        the same normalized claim text arrived >= DUPLICATE_FLOOD_THRESHOLD times in the run
"""
from . import contracts as C


def case_alerts(final, history, duplicate_count):
    fired = []
    if final["reason_code"] == C.RUN_EMPTY:
        fired.append(C.A2)
    states = [h["state"] for h in history]
    if any(s == C.SUBSTANCE and any(t in (C.UNVERIFIED, C.REFUTED) for t in states[i + 1:]) for i, s in enumerate(states)):
        fired.append(C.A1)
    if duplicate_count >= C.DUPLICATE_FLOOD_THRESHOLD:
        fired.append(C.A5)
    if final["state"] == C.REFUTED:
        fired.append(C.A4)
    if final["state"] in (C.UNVERIFIED, C.PENDING) and final["reason_code"] != C.RUN_EMPTY:
        fired.append(C.A3)
    fired = [r for r in C.ALERT_PRIORITY if r in fired]
    primary = fired[0] if fired else C.NO_ALERT
    return {"fired": fired, "primary": primary, "severity": C.ALERT_SEVERITY.get(primary, "none")}


def normalize_claim(text):
    return " ".join("".join(ch.lower() if ch.isalnum() else " " for ch in text).split())
