"""Sanitization sweep over a run dir: python -B -m app.core.sanitize_run <run_dir> [...]

Reuses demo_data/validate_demo_data.py's string checks (email/phone/SSN/card, absolute user paths,
credential prefixes, secret assignments, long hex outside hash fields, non-documentation IPs, handles,
non-allowlisted hosts). Fields whose name ends in 'sha256' are hash fields and may hold hex.
Exit 0 = clean, 1 = hits.
"""
import importlib.util
import json
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # never write demo_data/__pycache__ (outside this lane's write path)
_VALIDATOR = Path(__file__).resolve().parents[2] / "demo_data" / "validate_demo_data.py"


def _load_validator():
    spec = importlib.util.spec_from_file_location("demo_data_validator", _VALIDATOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rows(run_dir):
    for p in sorted(Path(run_dir).iterdir()):
        if p.suffix == ".jsonl":
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip():
                    yield f"{p.name}:{n}", json.loads(line)
        elif p.suffix == ".json":
            yield p.name, json.loads(p.read_text(encoding="utf-8"))


def sweep(run_dir):
    v = _load_validator()
    hits = []
    for where, obj in _rows(run_dir):
        for path, value in v.iter_strings(obj):
            if path.split(".")[-1].endswith("sha256"):
                continue
            # wrap each string as a mini-record so the validator's per-string checks apply unchanged
            for err in v.check_sanitization({"case_id": where, "x": value}):
                if "source.ref is outside" in err:
                    continue  # record-level check, not applicable to run artifacts
                hits.append(f"{where} {path}: {err.split(': ', 1)[-1]}")
    return hits


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    total = 0
    for d in argv:
        hits = sweep(d)
        total += len(hits)
        print(f"sanitization {'CLEAN' if not hits else 'HITS=' + str(len(hits))}  {Path(d).name}")
        for h in hits[:20]:
            print("  -", h)
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
