"""F1 claim ingest: read demo_data JSONL, validate every row against claims.schema.json, reject loudly.

Stdlib only. `jsonschema` is used when importable; otherwise a built-in validator covering the
draft-07 subset the schema actually uses (type, required, properties, additionalProperties,
enum, const, pattern, minLength, minItems, minimum, maximum, items, $ref).
"""
import json
import re
from pathlib import Path

from . import contracts as C


class IngestError(Exception):
    """A row failed validation. Message always names file, line, case_id and field path."""


def _type_ok(value, t):
    if isinstance(t, list):
        return any(_type_ok(value, x) for x in t)
    return {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "boolean": lambda v: isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "null": lambda v: v is None,
    }[t](value)


def _resolve(schema_root, node):
    while "$ref" in node:
        ref = node["$ref"]
        if not ref.startswith("#/"):
            raise IngestError(f"unsupported $ref {ref}")
        cur = schema_root
        for part in ref[2:].split("/"):
            cur = cur[part]
        node = cur
    return node


def builtin_validate(value, node, root, path="$"):
    """Return the first violation as 'path: message', or None."""
    node = _resolve(root, node)
    if "type" in node and not _type_ok(value, node["type"]):
        return f"{path}: expected type {node['type']}, got {type(value).__name__}"
    if "const" in node and value != node["const"]:
        return f"{path}: must equal {node['const']!r}"
    if "enum" in node and value not in node["enum"]:
        return f"{path}: {value!r} not in enum"
    if isinstance(value, str):
        if "minLength" in node and len(value) < node["minLength"]:
            return f"{path}: shorter than {node['minLength']}"
        if "pattern" in node and not re.search(node["pattern"], value):
            return f"{path}: does not match {node['pattern']}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in node and value < node["minimum"]:
            return f"{path}: below minimum {node['minimum']}"
        if "maximum" in node and value > node["maximum"]:
            return f"{path}: above maximum {node['maximum']}"
    if isinstance(value, list):
        if "minItems" in node and len(value) < node["minItems"]:
            return f"{path}: fewer than {node['minItems']} items"
        if "items" in node:
            for i, item in enumerate(value):
                err = builtin_validate(item, node["items"], root, f"{path}[{i}]")
                if err:
                    return err
    if isinstance(value, dict):
        for req in node.get("required", []):
            if req not in value:
                return f"{path}.{req}: required field missing"
        props = node.get("properties", {})
        if node.get("additionalProperties") is False:
            extra = sorted(set(value) - set(props))
            if extra:
                return f"{path}.{extra[0]}: additional property not allowed"
        for key, sub in props.items():
            if key in value:
                err = builtin_validate(value[key], sub, root, f"{path}.{key}")
                if err:
                    return err
    return None


def load_schema(data_dir):
    p = Path(data_dir) / "claims.schema.json"
    if not p.is_file():
        raise IngestError(f"schema not found: {p.name} in data dir")
    return json.loads(p.read_text(encoding="utf-8"))


def validate_record(record, schema):
    try:
        import jsonschema  # optional
        errs = sorted(jsonschema.Draft7Validator(schema).iter_errors(record), key=lambda e: list(e.path))
        if errs:
            e = errs[0]
            return "$." + ".".join(str(p) for p in e.path) + ": " + e.message
        return None
    except ImportError:
        return builtin_validate(record, schema, schema)


def load_records(data_dir, files=None):
    """Read and validate every row of every *.jsonl file in data_dir. Raises IngestError on the first bad row.

    Returns (records, provenance) where provenance lists (file_name, line_no, case_id) - names only,
    never absolute paths (sanitization rule).
    """
    data_dir = Path(data_dir)
    schema = load_schema(data_dir)
    paths = [data_dir / f for f in files] if files else sorted(data_dir.glob("*.jsonl"))
    if not paths:
        raise IngestError("no *.jsonl files in data dir")
    records, prov, seen = [], [], set()
    for p in paths:
        with open(p, "r", encoding="utf-8", newline="") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise IngestError(f"{p.name}:{line_no}: case_id=? field=$: invalid JSON ({exc.msg})")
                cid = rec.get("case_id", "?") if isinstance(rec, dict) else "?"
                err = validate_record(rec, schema)
                if err:
                    raise IngestError(f"{p.name}:{line_no}: case_id={cid} field={err}")
                if cid in seen:
                    raise IngestError(f"{p.name}:{line_no}: case_id={cid} field=$.case_id: duplicate case_id")
                seen.add(cid)
                records.append(rec)
                prov.append((p.name, line_no, cid))
    return records, prov


def engine_view(record):
    """Strip the answer key. The engine sees only source + assertions + receipts (minus relation)."""
    # The fixture's pre-extracted assertions are NOT passed through: decomposition is M1's job.
    # The records carry no raw source text, so the claim text M1 reads is the assertions joined.
    view = {k: record[k] for k in C.ENGINE_RECORD_FIELDS if k in record and k != "claims"}
    view["receipts"] = [{k: r[k] for k in C.ENGINE_RECEIPT_FIELDS} for r in record.get("receipts", [])]
    view["claim_text"] = " ".join(a["text"] for a in record.get("claims", []))
    return view


def select_suite(records, suite):
    if suite == "empty":
        return []           # the Oct-20 shape at run level: nothing ingested
    if suite == "all":
        return list(records)
    return [r for r in records if r["suite"] == suite]
