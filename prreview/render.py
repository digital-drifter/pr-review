import copy
import json
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional

from prreview.config import DATA_DIR
from prreview.models import Counts

SEVERITIES = ("critical", "should_fix", "nit")
LABELS = {"critical": "Critical", "should_fix": "Should fix", "nit": "Nit"}
ONE_LINE_MAX = 200


class ReviewInvalid(Exception):
    pass


def require_str(value: Any, field: str, allow_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise ReviewInvalid("%s must be a string" % field)
    if not allow_empty and not value.strip():
        raise ReviewInvalid("%s must not be empty" % field)


def validate_review(review: dict, context: dict) -> None:
    if not isinstance(review, dict):
        raise ReviewInvalid("review must be a JSON object")
    require_str(review.get("summary"), "summary")
    require_str(review.get("one_line"), "one_line")
    if len(review["one_line"]) > ONE_LINE_MAX:
        raise ReviewInvalid("one_line must be at most %d characters, got %d" % (ONE_LINE_MAX, len(review["one_line"])))
    if any(marker in review["one_line"].lower() for marker in ("://", "www.")):
        raise ReviewInvalid("one_line must not contain links (no :// or www.)")
    if "too_large" in review and not isinstance(review["too_large"], bool):
        raise ReviewInvalid("too_large must be a boolean")

    findings = review.get("findings")
    if not isinstance(findings, list):
        raise ReviewInvalid("findings must be a list")
    for i, item in enumerate(findings):
        prefix = "findings[%d]" % i
        if not isinstance(item, dict):
            raise ReviewInvalid("%s must be an object" % prefix)
        if item.get("severity") not in SEVERITIES:
            raise ReviewInvalid("%s.severity must be one of %s" % (prefix, ", ".join(SEVERITIES)))
        for key in ("title", "file", "breaks_when", "fix"):
            require_str(item.get(key), "%s.%s" % (prefix, key))
        line = item.get("line")
        if not isinstance(line, int) or isinstance(line, bool) or line < 1:
            raise ReviewInvalid("%s.line must be an integer >= 1" % prefix)
        if "fix_code" in item:
            require_str(item["fix_code"], "%s.fix_code" % prefix, allow_empty=True)
        if item["severity"] in ("critical", "should_fix") or "reproduce" in item:
            require_str(item.get("reproduce"), "%s.reproduce" % prefix)
        if "test_code" in item:
            require_str(item["test_code"], "%s.test_code" % prefix, allow_empty=True)

    fixed = review.get("fixed")
    if not isinstance(fixed, list) or not all(isinstance(x, str) for x in fixed):
        raise ReviewInvalid("fixed must be a list of finding ids")
    if fixed and context.get("mode") != "incremental" and not context.get("full_fallback"):
        raise ReviewInvalid("fixed must be empty in a full review")
    known = {f["id"] for f in context.get("earlier_findings", [])}
    for i, fid in enumerate(fixed):
        if fid not in known:
            raise ReviewInvalid("fixed[%d] %r is not an earlier finding id" % (i, fid))


def section_kind(context: dict) -> str:
    if context.get("mode") == "incremental":
        return "update"
    if context.get("full_fallback"):
        return "full_fallback"
    return "full"


def next_id_number(page: dict) -> int:
    numbers = [int(f["id"][1:]) for s in page["sections"] for f in s["findings"]]
    return max(numbers, default=0) + 1


def apply_review(page: Optional[dict], context: dict, review: dict, now: datetime) -> dict:
    validate_review(review, context)
    doc = copy.deepcopy(page) if page is not None else {"pr": {}, "sections": [], "fixed": {}}
    doc["pr"] = copy.deepcopy(context["pr"])
    stamp = now.isoformat(timespec="seconds")

    number = next_id_number(doc)
    ordered = []
    for severity in SEVERITIES:
        for item in review["findings"]:
            if item["severity"] == severity:
                ordered.append(dict(copy.deepcopy(item), id="F%d" % number))
                number += 1

    doc["sections"].insert(
        0,
        {
            "kind": section_kind(context),
            "at": stamp,
            "base": context.get("base"),
            "head": context["head"],
            "commit_count": context.get("commit_count", 0),
            "files_changed": context.get("files_changed", 0),
            "changed_lines": context.get("changed_lines", 0),
            "too_large": review.get("too_large", context.get("too_large", False)),
            "summary": review["summary"],
            "one_line": review["one_line"],
            "findings": ordered,
        },
    )
    for fid in review["fixed"]:
        doc["fixed"].setdefault(fid, stamp)
    return doc


def latest_counts(page: dict) -> Counts:
    sections = page.get("sections") or []
    findings = sections[0]["findings"] if sections else []
    return Counts(*[sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES])


def latest_fixed_count(page: dict) -> int:
    sections = page.get("sections") or []
    if not sections:
        return 0
    return sum(1 for stamp in page.get("fixed", {}).values() if stamp == sections[0]["at"])


def open_findings(page: Optional[dict]) -> List[dict]:
    if not page:
        return []
    fixed = page.get("fixed", {})
    keys = ("id", "severity", "title", "file", "line")
    return [{k: f[k] for k in keys} for s in page["sections"] for f in s["findings"] if f["id"] not in fixed]


def load_page(pr_id: int, data_dir: Path = DATA_DIR) -> Optional[dict]:
    path = data_dir / "pages" / ("%d.json" % pr_id)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def esc(value: Any) -> str:
    return escape(str(value), quote=True)


def long_date(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%b %-d, %Y %-I:%M %p")


def short_date(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%b %-d")


def plural(count: int, word: str) -> str:
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


CSS = """
:root{
  --bg:#f5f5f7;--card:#fff;--text:#1d1d1f;--muted:#5f5f66;--border:#d8d8de;--code:#ececf1;--accent:#0a5fd6;
  --crit:#c8102e;--should:#a85a00;--nit:#6e6e73;--fixed:#1a7f37;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg:#121214;--card:#1c1c20;--text:#f0f0f3;--muted:#a1a1aa;--border:#34343b;--code:#2a2a31;--accent:#6ea8ff;
    --crit:#d03048;--should:#a85a00;--nit:#6e6e73;--fixed:#4cc26b;
  }
}
:root[data-theme="dark"]{
  --bg:#121214;--card:#1c1c20;--text:#f0f0f3;--muted:#a1a1aa;--border:#34343b;--code:#2a2a31;--accent:#6ea8ff;
  --crit:#d03048;--should:#a85a00;--nit:#6e6e73;--fixed:#4cc26b;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;overflow-wrap:anywhere}
main{max-width:820px;margin:0 auto;padding:20px 16px 48px}
h1{font-size:1.35rem;line-height:1.3;margin:0 0 6px}
h2{font-size:1.05rem;margin:0 0 4px}
.meta{color:var(--muted);font-size:.85rem}
.meta a{color:var(--accent)}
details.section{margin-top:28px}
details.section>summary{cursor:pointer;list-style:none;display:flex;flex-wrap:wrap;align-items:baseline;gap:2px 12px;padding:6px 0}
details.section>summary::-webkit-details-marker{display:none}
details.section>summary::before{content:"";align-self:center;border-left:6px solid var(--muted);border-top:5px solid transparent;border-bottom:5px solid transparent;transition:transform .15s}
details.section[open]>summary::before{transform:rotate(90deg)}
@media (prefers-reduced-motion: reduce) {
  details.section>summary::before{transition:none}
}
summary:focus-visible,input.done:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.heading{font-size:1.05rem;font-weight:700}
.date{color:var(--muted);font-size:.8rem}
.progress{color:var(--muted);font-size:.8rem;font-variant-numeric:tabular-nums}
.head{display:flex;gap:10px;align-items:flex-start}
.head-text{min-width:0}
input.done{width:18px;height:18px;margin:2px 0 0;flex:none;accent-color:var(--accent);cursor:pointer}
input.done:disabled{cursor:default}
label.title{cursor:pointer}
.card.is-done{opacity:.6}
.card.is-done .title{text-decoration:line-through}
.summary{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin:10px 0 14px}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:12px;margin-bottom:12px}
.card.is-fixed{opacity:.55}
.badge{display:inline-block;font-size:.68rem;font-weight:700;letter-spacing:.04em;text-transform:uppercase;padding:2px 7px;border-radius:4px;color:#fff;margin-right:6px;vertical-align:1px}
.badge.critical{background:var(--crit)}
.badge.should_fix{background:var(--should)}
.badge.nit{background:var(--nit)}
.badge.fixed{background:var(--fixed);color:#fff}
:root[data-theme="dark"] .badge.fixed{color:#06240f}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) .badge.fixed{color:#06240f}
}
.title{font-weight:700}
.where{margin:6px 0}
code{font:.8rem ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;background:var(--code);padding:1px 5px;border-radius:3px}
.k{font-weight:600}
.row{margin-top:6px}
.steps{white-space:pre-wrap;margin-top:2px}
pre{background:var(--code);border-radius:6px;padding:8px 10px;margin:6px 0 0;font:.8rem/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere}
.empty,.notice{color:var(--muted);font-style:italic}
"""


def render_header(page: dict) -> str:
    pr = page["pr"]
    latest = page["sections"][0] if page["sections"] else None
    parts = [
        esc(pr.get("author", "")),
        "%s → %s" % (esc(pr.get("source_branch", "")), esc(pr.get("destination_branch", ""))),
    ]
    if latest:
        parts.append(plural(latest["files_changed"], "file"))
        parts.append("reviewed at %s" % esc(latest["head"][:7]))
    link = pr.get("link", "")
    if isinstance(link, str) and link.startswith("https://"):
        parts.append('<a href="%s">View PR</a>' % esc(link))
    return '<header><h1>PR #%s %s</h1><div class="meta">%s</div></header>' % (
        esc(pr.get("id", "")),
        esc(pr.get("title", "")),
        " &middot; ".join(parts),
    )


def render_finding(item: dict, fixed: Dict[str, str]) -> str:
    stamp = fixed.get(item["id"])
    fid = esc(item["id"])
    box = '<input type="checkbox" class="done" id="done-%s" data-key="done-%s"%s>' % (
        fid, fid, " checked disabled" if stamp else ""
    )
    badges = '<span class="badge %s">%s</span>' % (item["severity"], LABELS[item["severity"]])
    if stamp:
        badges += '<span class="badge fixed">Fixed %s</span>' % esc(short_date(stamp))
    code = ""
    if item.get("fix_code"):
        code = "<pre>%s</pre>" % esc(item["fix_code"])
    if item.get("reproduce"):
        code += '<div class="row"><span class="k">Test / reproduce:</span><div class="steps">%s</div></div>' % esc(
            item["reproduce"]
        )
    if item.get("test_code"):
        code += "<pre>%s</pre>" % esc(item["test_code"])
    return (
        '<div class="card%s" id="%s"><div class="head">%s<div class="head-text">%s'
        '<label class="title" for="done-%s">%s</label></div></div>'
        '<div class="where"><code>%s:%s</code></div>'
        '<div class="row"><span class="k">Breaks when:</span> %s</div>'
        '<div class="row"><span class="k">Fix:</span> %s</div>%s</div>'
        % (
            " is-fixed" if stamp else "",
            fid,
            box,
            badges,
            fid,
            esc(item["title"]),
            esc(item["file"]),
            esc(item["line"]),
            esc(item["breaks_when"]),
            esc(item["fix"]),
            code,
        )
    )


def section_heading(section: dict) -> str:
    if section["kind"] == "update":
        base = (section.get("base") or "")[:7]
        return "Update: %s (%s → %s)" % (plural(section["commit_count"], "new commit"), base, section["head"][:7])
    if section["kind"] == "full_fallback":
        return "Full re-review: branch was rebased or force-pushed"
    return "Full review"


def render_section(section: dict, fixed: Dict[str, str], expanded: bool) -> str:
    body = ['<div class="summary">%s</div>' % esc(section["summary"])]
    if section.get("too_large"):
        body.append(
            '<p class="notice">Too large for a full auto-review (%s changed lines). Summary only.</p>'
            % esc(section["changed_lines"])
        )
    elif not section["findings"]:
        body.append('<p class="empty">No issues found</p>')
    body.extend(render_finding(f, fixed) for f in section["findings"])
    total = len(section["findings"])
    progress = ""
    if total:
        done = sum(1 for f in section["findings"] if f["id"] in fixed)
        progress = '<span class="progress">%d of %d done</span>' % (done, total)
    return (
        '<details class="section"%s><summary><span class="heading">%s</span><span class="date">%s</span>%s</summary>'
        "%s</details>"
        % (
            " open" if expanded else "",
            esc(section_heading(section)),
            esc(long_date(section["at"])),
            progress,
            "".join(body),
        )
    )


CHECKLIST_SCRIPT = """
(function () {
  var store = null;
  try { store = window.localStorage; } catch (e) { store = null; }
  function remembered(key) {
    try { return store !== null && store.getItem(key) === "1"; } catch (e) { return false; }
  }
  function remember(key, done) {
    try {
      if (store === null) { return; }
      if (done) { store.setItem(key, "1"); } else { store.removeItem(key); }
    } catch (e) {}
  }
  function mark(box) {
    var card = box.closest(".card");
    if (card) { card.classList.toggle("is-done", box.checked); }
  }
  function progress() {
    document.querySelectorAll("details.section").forEach(function (section) {
      var out = section.querySelector(".progress");
      if (!out) { return; }
      var all = section.querySelectorAll("input.done").length;
      var done = section.querySelectorAll("input.done:checked").length;
      out.textContent = done + " of " + all + " done";
    });
  }
  document.querySelectorAll("input.done").forEach(function (box) {
    if (!box.disabled && remembered(box.dataset.key)) { box.checked = true; }
    mark(box);
    box.addEventListener("change", function () {
      remember(box.dataset.key, box.checked);
      mark(box);
      progress();
    });
  });
  progress();
})();
"""


def render_html(page: dict) -> str:
    fixed = page.get("fixed", {})
    sections = "".join(render_section(s, fixed, i == 0) for i, s in enumerate(page["sections"]))
    return (
        "<!doctype html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>PR %s Review</title>\n<style>%s</style>\n</head>\n"
        "<body><main>%s%s</main><script>%s</script></body>\n</html>\n"
        % (esc(page["pr"]["id"]), CSS, render_header(page), sections, CHECKLIST_SCRIPT)
    )
