/* In-browser ledger re-check, independent of the Python verifier.
 *
 * Recomputes every row_sha256 as sha256(canonical(row without row_sha256)), where canonical() mirrors
 * app/core/ledger.py: json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).
 * Also checks prev_sha256 links and seq continuity.
 *
 * Works in a browser (window.LedgerCheck) and in node >= 19 (module.exports) so test_ui.py can run the
 * exact same code against every run dir with a positive and a negative control.
 */
(function (root) {
  "use strict";
  var ZERO = "0000000000000000000000000000000000000000000000000000000000000000";

  function canonical(v) {
    if (v === null || typeof v !== "object") {
      if (typeof v === "number" && !Number.isInteger(v)) {
        // Python prints floats differently from JS (1.0 vs 1); refuse rather than guess.
        throw new Error("non-integer number in ledger row; canonical form not reproducible in JS");
      }
      return JSON.stringify(v);
    }
    if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
    var keys = Object.keys(v).sort();
    return "{" + keys.map(function (k) { return JSON.stringify(k) + ":" + canonical(v[k]); }).join(",") + "}";
  }

  function hex(buf) {
    var b = new Uint8Array(buf), s = "";
    for (var i = 0; i < b.length; i++) s += (b[i] < 16 ? "0" : "") + b[i].toString(16);
    return s;
  }

  function subtle() {
    var c = (typeof crypto !== "undefined" && crypto) || (typeof globalThis !== "undefined" && globalThis.crypto);
    return c && c.subtle ? c.subtle : null;
  }

  async function sha256Hex(text) {
    var s = subtle();
    if (!s) throw new Error("WebCrypto unavailable");
    return hex(await s.digest("SHA-256", new TextEncoder().encode(text)));
  }

  /* lines: array of raw ledger.jsonl lines. Returns {ok, rows, problems[]}. */
  async function verifyLines(lines) {
    var problems = [], prev = ZERO, expectSeq = 1, n = 0;
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      if (!line || !line.trim()) continue;
      n++;
      var row;
      try { row = JSON.parse(line); } catch (e) { problems.push("line " + (i + 1) + ": not JSON"); continue; }
      if (row.seq !== expectSeq) problems.push("line " + (i + 1) + ": seq " + row.seq + " expected " + expectSeq);
      if (row.prev_sha256 !== prev) problems.push("line " + (i + 1) + ": prev_sha256 does not link to previous row");
      var body = {};
      Object.keys(row).forEach(function (k) { if (k !== "row_sha256") body[k] = row[k]; });
      var got;
      try { got = await sha256Hex(canonical(body)); } catch (e) { problems.push("line " + (i + 1) + ": " + e.message); continue; }
      if (got !== row.row_sha256) problems.push("line " + (i + 1) + ": row_sha256 mismatch (row was edited)");
      prev = row.row_sha256;
      expectSeq = (typeof row.seq === "number" ? row.seq : expectSeq) + 1;
    }
    if (n === 0) problems.push("ledger is empty");
    return { ok: problems.length === 0, rows: n, problems: problems };
  }

  var api = { canonical: canonical, verifyLines: verifyLines, available: function () { return !!subtle(); } };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.LedgerCheck = api;
})(typeof window !== "undefined" ? window : this);
