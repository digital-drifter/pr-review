import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from prreview.models import Counts
from prreview.render import (
    ReviewInvalid,
    apply_review,
    latest_counts,
    latest_fixed_count,
    load_page,
    open_findings,
    render_html,
    validate_review,
)

T1 = datetime(2026, 10, 27, 14, 14, 0)
T2 = datetime(2026, 10, 28, 9, 5, 0)


def context(mode: str = "full", fallback: bool = False, earlier: Optional[List[dict]] = None, head: str = "d" * 40) -> dict:
    return {
        "pr": {
            "id": 2834,
            "title": "Move invoice lookups",
            "description": "desc",
            "author": "Jordan",
            "draft": False,
            "head": head[:12],
            "source_branch": "feature/bridge",
            "destination_branch": "master",
            "link": "https://bitbucket.org/acme/widgets/pull-requests/2834",
        },
        "mode": mode,
        "full_fallback": fallback,
        "base": "a" * 40,
        "head": head,
        "files_changed": 9,
        "changed_lines": 321,
        "commit_count": 3,
        "too_large": False,
        "earlier_findings": earlier or [],
    }


def finding(severity: str = "nit", title: str = "A title", **extra: object) -> dict:
    data = {
        "severity": severity,
        "title": title,
        "file": "app/Foo.php",
        "line": 12,
        "breaks_when": "it breaks",
        "fix": "fix it",
    }
    if severity in ("critical", "should_fix"):
        data["reproduce"] = "1. Open the page\n2. See the error"
    data.update(extra)
    return data


def review(findings: Optional[List[dict]] = None, fixed: Optional[List[str]] = None, **extra: object) -> dict:
    data = {"summary": "Summary text", "one_line": "One line", "findings": findings or [], "fixed": fixed or []}
    data.update(extra)
    return data


def two_section_page() -> dict:
    first = apply_review(None, context(), review([finding("nit", "N"), finding("critical", "C"), finding("should_fix", "S")]), T1)
    earlier = [{"id": f["id"], "severity": f["severity"], "title": f["title"], "file": f["file"], "line": f["line"]} for f in first["sections"][0]["findings"]]
    return apply_review(first, context("incremental", earlier=earlier, head="e" * 40), review([finding("should_fix", "New one")], ["F2"]), T2)


class RenderTest(unittest.TestCase):
    def test_first_review_assigns_ids_and_orders_by_severity(self) -> None:
        page = apply_review(None, context(), review([finding("nit", "N"), finding("critical", "C"), finding("should_fix", "S")]), T1)
        section = page["sections"][0]
        self.assertEqual("full", section["kind"])
        self.assertEqual([("F1", "C"), ("F2", "S"), ("F3", "N")], [(f["id"], f["title"]) for f in section["findings"]])
        self.assertEqual("2026-10-27T14:14:00", section["at"])
        self.assertEqual(2834, page["pr"]["id"])

    def test_update_section_goes_on_top_and_ids_continue(self) -> None:
        page = two_section_page()
        self.assertEqual("update", page["sections"][0]["kind"])
        self.assertEqual("full", page["sections"][1]["kind"])
        self.assertEqual("F4", page["sections"][0]["findings"][0]["id"])
        self.assertEqual({"F2": "2026-10-28T09:05:00"}, page["fixed"])

    def test_apply_review_does_not_mutate_inputs(self) -> None:
        page = two_section_page()
        ctx = context("incremental", earlier=[{"id": "F1", "severity": "nit", "title": "t", "file": "f", "line": 1}])
        rev = review([finding()], ["F1"])
        before = copy.deepcopy((page, ctx, rev))
        apply_review(page, ctx, rev, T2)
        self.assertEqual(before, (page, ctx, rev))

    def test_fixed_ids_are_marked_and_rendered(self) -> None:
        html = render_html(two_section_page())
        self.assertIn("Fixed Oct 28", html)
        start = html.index('id="F2"')
        self.assertIn("Fixed Oct 28", html[start : start + 600])
        self.assertNotIn("Fixed Oct 28", html[html.index('id="F1"') : html.index('id="F2"')])

    def test_no_findings_renders_no_issues_found(self) -> None:
        html = render_html(apply_review(None, context(), review(), T1))
        self.assertIn("No issues found", html)

    def test_too_large_renders_notice(self) -> None:
        html = render_html(apply_review(None, context(), review(too_large=True), T1))
        self.assertIn("Too large for a full auto-review (321 changed lines). Summary only.", html)
        self.assertNotIn("No issues found", html)

    def test_full_fallback_section_heading(self) -> None:
        page = apply_review(None, context(fallback=True), review(), T1)
        self.assertEqual("full_fallback", page["sections"][0]["kind"])
        self.assertIn("Full re-review: branch was rebased or force-pushed", render_html(page))

    def test_update_heading_and_dates(self) -> None:
        html = render_html(two_section_page())
        self.assertIn("Update: 3 new commits (aaaaaaa → eeeeeee)", html)
        self.assertIn("Oct 28, 2026 9:05 AM", html)
        self.assertIn("Oct 27, 2026 2:14 PM", html)

    def test_critical_and_should_fix_require_test_or_reproduction_steps(self) -> None:
        ctx = context()
        missing_should_fix = finding("should_fix")
        del missing_should_fix["reproduce"]
        missing_critical = finding("critical")
        del missing_critical["reproduce"]
        for item, field in (
            (missing_should_fix, "findings[0].reproduce"),
            (missing_critical, "findings[0].reproduce"),
            (finding("should_fix", reproduce="  "), "findings[0].reproduce"),
            (finding("should_fix", reproduce=3), "findings[0].reproduce"),
            (finding("nit", reproduce=""), "findings[0].reproduce"),
            (finding("should_fix", test_code=5), "findings[0].test_code"),
        ):
            with self.subTest(field=field, item=item):
                with self.assertRaises(ReviewInvalid) as raised:
                    validate_review(review([item]), ctx)
                self.assertIn(field, str(raised.exception))
        validate_review(review([finding("should_fix"), finding("critical"), finding("nit")]), ctx)

    def test_reproduction_steps_and_test_code_render_escaped(self) -> None:
        item = finding(
            "should_fix", reproduce="1. Run <cmd>\n2. See & compare", test_code="<?php public function testIt(): void {}",
        )
        html = render_html(apply_review(None, context(), review([item]), T1))
        self.assertIn("Test / reproduce:", html)
        self.assertIn("1. Run &lt;cmd&gt;\n2. See &amp; compare", html)
        self.assertIn("<pre>&lt;?php public function testIt(): void {}</pre>", html)
        self.assertIn(".steps{white-space:pre-wrap", html)

    def test_findings_without_reproduction_render_without_that_row(self) -> None:
        html = render_html(apply_review(None, context(), review([finding("nit")]), T1))
        self.assertNotIn("Test / reproduce:", html)

    def test_hostile_text_is_escaped(self) -> None:
        evil = "<script>alert(1)</script>"
        ctx = context()
        ctx["pr"]["title"] = evil
        ctx["pr"]["author"] = evil
        ctx["pr"]["source_branch"] = evil
        ctx["pr"]["link"] = 'https://x.test/"><script>alert(1)</script>'
        rev = review([finding("critical", evil, file=evil, breaks_when=evil, fix=evil, fix_code=evil)], summary=evil)
        html = render_html(apply_review(None, ctx, rev, T1))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn('"><script>', html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)
        self.assertEqual(html.count("<script>"), 1)

    def test_non_https_link_is_omitted(self) -> None:
        ctx = context()
        ctx["pr"]["link"] = "javascript:alert(1)"
        html = render_html(apply_review(None, ctx, review(), T1))
        self.assertNotIn("javascript:", html)
        self.assertNotIn("View PR", html)

    def test_rejects_bad_severity_missing_fields_and_unknown_fixed_ids(self) -> None:
        ctx = context("incremental", earlier=[{"id": "F1", "severity": "nit", "title": "t", "file": "f", "line": 1}])
        cases = [
            (review([finding(), finding(), finding("urgent")]), "findings[2].severity"),
            (review([finding(title="")]), "findings[0].title"),
            (review([finding(line=0)]), "findings[0].line"),
            (review([finding(line="3")]), "findings[0].line"),
            (review([finding(line=True)]), "findings[0].line"),
            (review([finding(fix_code=5)]), "findings[0].fix_code"),
            (review(one_line="x" * 201), "one_line"),
            (review(fixed=["F9"]), "fixed"),
            (review(too_large="yes"), "too_large"),
            ({"summary": "s", "one_line": "o", "fixed": []}, "findings"),
        ]
        for rev, field in cases:
            with self.subTest(field=field):
                with self.assertRaises(ReviewInvalid) as raised:
                    validate_review(rev, ctx)
                self.assertIn(field, str(raised.exception))
        validate_review(review([finding(fix_code="x")], ["F1"], too_large=False), ctx)

    def test_one_line_rejects_links(self) -> None:
        for one_line in ("See https://evil.test/x", "javascript://x", "Visit WWW.evil.test now", "slack://open"):
            with self.subTest(one_line=one_line):
                with self.assertRaisesRegex(ReviewInvalid, "one_line must not contain links"):
                    validate_review(review(one_line=one_line), context())
        validate_review(review(one_line="Moves lookups to the bridge: no issues."), context())

    def test_full_mode_rejects_fixed(self) -> None:
        with self.assertRaises(ReviewInvalid) as raised:
            validate_review(review(fixed=["F1"]), context(earlier=[{"id": "F1", "severity": "nit", "title": "t", "file": "f", "line": 1}]))
        self.assertIn("fixed", str(raised.exception))

    def test_full_fallback_marks_earlier_findings_fixed(self) -> None:
        first = apply_review(None, context(), review([finding("critical", "C"), finding("nit", "N")]), T1)
        earlier = open_findings(first)
        ctx = context(fallback=True, earlier=earlier, head="e" * 40)

        page = apply_review(first, ctx, review([finding("should_fix", "New")], ["F1"]), T2)

        self.assertEqual(page["sections"][0]["kind"], "full_fallback")
        self.assertEqual(page["fixed"], {"F1": "2026-10-28T09:05:00"})
        self.assertEqual(latest_fixed_count(page), 1)
        self.assertEqual(sorted(f["id"] for f in open_findings(page)), ["F2", "F3"])
        with self.assertRaisesRegex(ReviewInvalid, "fixed\\[0\\]"):
            validate_review(review(fixed=["F9"]), ctx)

    def test_sections_are_collapsible_with_only_the_newest_open(self) -> None:
        html = render_html(two_section_page())
        self.assertEqual(html.count('<details class="section"'), 2)
        self.assertEqual(html.count('<details class="section" open>'), 1)
        self.assertLess(html.index('<details class="section" open>'), html.index('<details class="section">'))
        self.assertEqual(html.count("<summary>"), 2)

    def test_every_finding_is_a_checklist_item_with_a_stable_id(self) -> None:
        html = render_html(two_section_page())
        for fid in ("F1", "F2", "F3", "F4"):
            with self.subTest(fid):
                self.assertIn('<input type="checkbox" class="done" id="done-%s" data-key="done-%s"' % (fid, fid), html)
                self.assertIn('<label class="title" for="done-%s">' % fid, html)
        self.assertEqual(html.count('class="done"'), 4)

    def test_fixed_findings_are_checked_and_locked(self) -> None:
        html = render_html(two_section_page())
        self.assertIn('id="done-F2" data-key="done-F2" checked disabled', html)
        self.assertNotIn('id="done-F1" data-key="done-F1" checked', html)

    def test_sections_show_checklist_progress(self) -> None:
        html = render_html(two_section_page())
        newest, older = html.split('<details class="section">')
        self.assertIn('<span class="progress">0 of 1 done</span>', newest)
        self.assertIn('<span class="progress">1 of 3 done</span>', older)
        empty = render_html(apply_review(None, context(), review(), T1))
        self.assertNotIn('class="progress"', empty)

    def test_checklist_state_is_remembered_in_this_browser_with_guards(self) -> None:
        html = render_html(two_section_page())
        script = html.split("<script>", 1)[1].split("</script>", 1)[0]
        self.assertIn("localStorage", script)
        self.assertGreaterEqual(script.count("try"), 2)
        self.assertIn("is-done", script)
        self.assertIn(".card.is-done .title{text-decoration:line-through", html)

    def test_page_contract(self) -> None:
        html = render_html(two_section_page())
        self.assertIn("<title>PR 2834 Review</title>", html)
        self.assertIn(":root{", html.replace(" ", ""))
        self.assertIn('@media (prefers-color-scheme: dark)', html)
        self.assertIn(':root:not([data-theme="light"])', html)
        self.assertIn(':root[data-theme="dark"]', html)
        self.assertIn('<meta name="viewport" content="width=device-width, initial-scale=1">', html)
        self.assertIn('<meta charset="utf-8">', html)
        self.assertNotIn("<script src", html)
        self.assertEqual(html.count("<script>"), 1)
        self.assertNotIn("—", html)
        self.assertNotIn("http://", html)

    def test_latest_counts_and_fixed_count(self) -> None:
        page = two_section_page()
        self.assertEqual(Counts(0, 1, 0), latest_counts(page))
        self.assertEqual(1, latest_fixed_count(page))
        first = apply_review(None, context(), review([finding("critical"), finding("nit"), finding("nit")]), T1)
        self.assertEqual(Counts(1, 0, 2), latest_counts(first))
        self.assertEqual(0, latest_fixed_count(first))

    def test_open_findings_excludes_fixed(self) -> None:
        opened = open_findings(two_section_page())
        self.assertEqual(["F1", "F3", "F4"], sorted(f["id"] for f in opened))
        self.assertEqual({"id", "severity", "title", "file", "line"}, set(opened[0]))
        self.assertEqual([], open_findings(None))

    def test_load_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            self.assertIsNone(load_page(7, data_dir))
            (data_dir / "pages").mkdir()
            (data_dir / "pages" / "7.json").write_text(json.dumps({"sections": []}))
            self.assertEqual({"sections": []}, load_page(7, data_dir))


if __name__ == "__main__":
    unittest.main()
