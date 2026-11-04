/* Page behaviour. The page is fully readable with JavaScript off; this only adds:
 *  - one run visible at a time (follows the URL hash, so ledger links across runs still work)
 *  - opening the <details> that contains a linked ledger row / case, then scrolling to it
 *  - the in-browser WebCrypto re-check of every row_sha256 (ledger_check.js)
 */
(function () {
  "use strict";
  var sections = Array.prototype.slice.call(document.querySelectorAll("section.run"));
  var links = Array.prototype.slice.call(document.querySelectorAll("nav .runlink"));

  function show(runId) {
    sections.forEach(function (s) { s.classList.toggle("hidden", s.getAttribute("data-run") !== runId); });
    links.forEach(function (a) { a.classList.toggle("active", a.getAttribute("data-run") === runId); });
  }

  function follow() {
    var id = decodeURIComponent((location.hash || "").slice(1));
    var el = id && document.getElementById(id);
    if (!el) { if (sections.length) show(sections[0].getAttribute("data-run")); return; }
    var sec = el.closest("section.run");
    if (sec) show(sec.getAttribute("data-run"));
    for (var p = el; p; p = p.parentElement) if (p.tagName === "DETAILS") p.open = true;
    el.scrollIntoView({ block: "center" });
  }

  window.addEventListener("hashchange", follow);
  follow();

  function setResult(span, cls, glyph, text) {
    span.innerHTML = "";
    var b = document.createElement("span");
    b.className = "badge " + cls;
    b.textContent = glyph + " " + text;
    span.appendChild(b);
  }

  if (!window.LedgerCheck) return;
  document.querySelectorAll("script.ledger-lines").forEach(function (node) {
    var run = node.getAttribute("data-run");
    var span = document.querySelector('.jscheck[data-run="' + run + '"]');
    if (!span) return;
    if (!window.LedgerCheck.available()) {
      setResult(span, "pend", "○", "unavailable in this browser (build-time result above stands)");
      return;
    }
    setResult(span, "pend", "○", "checking…");
    var lines;
    try { lines = JSON.parse(node.textContent); } catch (e) { setResult(span, "bad", "✖", "FAIL: embedded ledger unreadable"); return; }
    window.LedgerCheck.verifyLines(lines).then(function (r) {
      if (r.ok) setResult(span, "ok", "✔", "PASS: " + r.rows + " rows recomputed, chain links");
      else setResult(span, "bad", "✖", "FAIL: " + r.problems.slice(0, 3).join("; "));
    }, function (e) { setResult(span, "pend", "○", "could not run: " + e.message); });
  });
})();
