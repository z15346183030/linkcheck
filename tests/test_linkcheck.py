#!/usr/bin/env python3
"""Tests for linkcheck. Run with: python -m unittest discover -s tests"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import linkcheck as lc  # noqa: E402

PAGE = b"""<!doctype html><html><body>
<h2 id="install">Install</h2>
<a name="legacy">old</a>
<h2 id="usage">Usage</h2>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", headers=None):
        self.send_response(code)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def do_HEAD(self):
        self.route()

    def do_GET(self):
        self.route()

    def route(self):
        path = self.path.split("?")[0]
        table = {
            "/": (200, b"<html><body>home</body></html>", {"content-type": "text/html"}),
            "/ok": (200, b"ok", {}),
            "/missing": (404, b"no", {}),
            "/gone": (410, b"gone", {}),
            "/forbidden": (403, b"cf", {"server": "cloudflare"}),
            "/ratelimit": (429, b"slow down", {"retry-after": "60"}),
            "/unavailable": (503, b"later", {}),
            "/servererror": (500, b"boom", {}),
            "/redirect": (302, b"", {"location": "/ok"}),
            "/loop": (302, b"", {"location": "/loop"}),
            "/page": (200, PAGE, {"content-type": "text/html; charset=utf-8"}),
            "/doc.pdf": (200, b"%PDF-1.4", {"content-type": "application/pdf"}),
        }
        if path == "/head405":
            self._send(405 if self.command == "HEAD" else 200, b"only-get")
            return
        if path == "/head404":
            self._send(404 if self.command == "HEAD" else 200, b"head-lies")
            return
        if path == "/slow":
            time.sleep(3)
            self._send(200, b"slow")
            return
        if path == "/bigpage":
            # anchor sits past the 512 KB read cap: absence here proves nothing
            body = b"<html><body>" + b"x" * 600_000 + b'<h2 id="late">late</h2></body></html>'
            self._send(200, body, {"content-type": "text/html"})
            return
        if path == "/bigpage-early":
            body = b'<html><body><h2 id="early">e</h2>' + b"x" * 600_000 + b"</body></html>"
            self._send(200, body, {"content-type": "text/html"})
            return
        code, body, headers = table.get(path, (404, b"unknown", {}))
        self._send(code, body, headers)


class ServerCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"


# ---------------------------------------------------------------------------
# Slugs and anchors
# ---------------------------------------------------------------------------

class TestSlugify(unittest.TestCase):
    def test_lowercases_and_hyphenates(self):
        self.assertEqual(lc.slugify("Hello World"), "hello-world")

    def test_drops_punctuation(self):
        self.assertEqual(lc.slugify("What's new? (v2)"), "whats-new-v2")

    def test_keeps_underscores_and_hyphens(self):
        self.assertEqual(lc.slugify("my_var-name"), "my_var-name")

    def test_code_span_keeps_text(self):
        self.assertEqual(lc.slugify("The `--json` flag"), "the---json-flag")

    def test_image_uses_alt_text(self):
        self.assertEqual(lc.slugify("![logo](x.png) Intro"), "logo-intro")

    def test_cjk_survives(self):
        self.assertEqual(lc.slugify("第一部分 安装"), "第一部分-安装")

    def test_strips_inline_html(self):
        self.assertEqual(lc.slugify("Use <b>bold</b>"), "use-bold")

    def test_trailing_hashes_removed_by_caller(self):
        self.assertEqual(lc.slugify("# heading #".strip("# ")), "heading")


class TestMarkdownAnchors(unittest.TestCase):
    def test_atx_headings(self):
        anchors = lc.anchors_in_markdown("# One\n\n## Two Words\n")
        self.assertEqual(anchors, {"one", "two-words"})

    def test_setext_headings(self):
        anchors = lc.anchors_in_markdown("Title\n=====\n\nSub\n---\n")
        self.assertEqual(anchors, {"title", "sub"})

    def test_duplicates_get_numeric_suffix(self):
        anchors = lc.anchors_in_markdown("# A\n# A\n# A\n")
        self.assertEqual(anchors, {"a", "a-1", "a-2"})

    def test_fenced_block_headings_ignored(self):
        anchors = lc.anchors_in_markdown("# Real\n\n```\n# Fake\n```\n")
        self.assertEqual(anchors, {"real"})

    def test_front_matter_is_not_a_heading(self):
        anchors = lc.anchors_in_markdown("---\ntitle: x\n---\n\n# Real\n")
        self.assertEqual(anchors, {"real"})

    def test_html_ids_inside_markdown(self):
        anchors = lc.anchors_in_markdown('# Real\n\n<a id="anchor-one"></a>\n')
        self.assertIn("anchor-one", anchors)

    def test_horizontal_rule_is_not_a_heading(self):
        anchors = lc.anchors_in_markdown("# Real\n\n---\n\nmore text\n")
        self.assertEqual(anchors, {"real"})

    def test_empty_document(self):
        self.assertEqual(lc.anchors_in_markdown(""), set())


class TestHtmlAnchors(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(lc.anchors_in_html('<h1 id="a">x</h1><div id="b"></div>'), {"a", "b"})

    def test_anchor_names(self):
        self.assertEqual(lc.anchors_in_html('<a name="old"></a>'), {"old"})

    def test_ids_lowercased(self):
        self.assertEqual(lc.anchors_in_html('<div id="MixedCase"></div>'), {"mixedcase"})


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

class TestExtractMarkdown(unittest.TestCase):
    def targets(self, text):
        return [f.target for f in lc.extract_markdown(text, "doc.md")]

    def test_inline_link(self):
        self.assertEqual(self.targets("see [docs](https://example.com/a)"), ["https://example.com/a"])

    def test_image(self):
        self.assertEqual(self.targets("![alt](img/x.png)"), ["img/x.png"])

    def test_reference_definition(self):
        self.assertEqual(self.targets("[id]: https://example.com\n"), ["https://example.com"])

    def test_reference_use_is_not_a_target(self):
        self.assertEqual(self.targets("see [text][id] only\n"), [])

    def test_autolink(self):
        self.assertEqual(self.targets("go <https://example.com>"), ["https://example.com"])

    def test_title_is_stripped(self):
        self.assertEqual(self.targets('[x](https://example.com "the title")'), ["https://example.com"])

    def test_angle_brackets_around_url(self):
        self.assertEqual(self.targets("[x](<https://example.com/a b>)"), ["https://example.com/a b"])

    def test_nested_parens_in_url(self):
        self.assertEqual(self.targets("[x](https://en.wikipedia.org/wiki/Foo_(bar))"),
                         ["https://en.wikipedia.org/wiki/Foo_(bar)"])

    def test_link_in_inline_code_is_ignored(self):
        self.assertEqual(self.targets("write `[x](nope.md)` like this"), [])

    def test_link_in_fenced_block_is_ignored(self):
        self.assertEqual(self.targets("```\n[x](nope.md)\n```\n"), [])

    def test_link_in_tilde_fence_is_ignored(self):
        self.assertEqual(self.targets("~~~\n[x](nope.md)\n~~~\n"), [])

    def test_inline_html_in_markdown(self):
        self.assertEqual(self.targets('<img src="pic.png">'), ["pic.png"])

    def test_line_numbers(self):
        found = lc.extract_markdown("a\n\n[b](c.md)\n", "doc.md")
        self.assertEqual(found[0].line, 3)

    def test_columns_point_at_the_label(self):
        found = lc.extract_markdown("- [b](c.md)", "doc.md")
        self.assertEqual(found[0].col, 3)

    def test_two_links_on_one_line(self):
        self.assertEqual(self.targets("[a](1.md) and [b](2.md)"), ["1.md", "2.md"])

    def test_empty_target_skipped(self):
        self.assertEqual(self.targets("[x]()"), [])


class TestExtractHtml(unittest.TestCase):
    def test_href_and_src(self):
        found = lc.extract_html('<a href="a.html">a</a><img src="b.png">', "p.html")
        self.assertEqual([f.target for f in found], ["a.html", "b.png"])

    def test_line_numbers(self):
        found = lc.extract_html('<p>x</p>\n<a href="a.html">a</a>', "p.html")
        self.assertEqual(found[0].line, 2)

    def test_ignores_text_that_looks_like_a_link(self):
        self.assertEqual(lc.extract_html("<p>[x](y.md)</p>", "p.html"), [])


class TestDispatch(unittest.TestCase):
    def test_html_file_uses_html_parser(self):
        found = lc.extract('<a href="x.html">x</a>', "a.html")
        self.assertEqual(found[0].target, "x.html")

    def test_markdown_file_uses_markdown_parser(self):
        found = lc.extract("[x](y.md)", "a.md")
        self.assertEqual(found[0].target, "y.md")


# ---------------------------------------------------------------------------
# Resolving local paths
# ---------------------------------------------------------------------------

class TestResolveLocal(unittest.TestCase):
    def test_relative(self):
        got = lc.resolve_local("a/b.md", os.path.join("root", "docs"))
        self.assertEqual(got, os.path.normpath(os.path.join("root", "docs", "a", "b.md")))

    def test_percent_encoded_space(self):
        got = lc.resolve_local("my%20file.md", "root")
        self.assertEqual(got, os.path.normpath(os.path.join("root", "my file.md")))

    def test_query_string_dropped(self):
        got = lc.resolve_local("a.md?x=1", "root")
        self.assertEqual(got, os.path.normpath(os.path.join("root", "a.md")))

    def test_tilde_expands(self):
        got = lc.resolve_local("~/x.md", "root")
        self.assertFalse("~" in got)

    def test_absolute_kept(self):
        absolute = os.path.abspath(os.path.join("root", "x.md"))
        self.assertEqual(lc.resolve_local(absolute, "other"), os.path.normpath(absolute))


# ---------------------------------------------------------------------------
# Status classification
# ---------------------------------------------------------------------------

class TestClassifyStatus(unittest.TestCase):
    def test_2xx_is_ok(self):
        for code in (200, 201, 204, 206):
            self.assertEqual(lc.classify_status(code), lc.OK, code)

    def test_404_410_451_are_dead(self):
        for code in (404, 410, 451):
            self.assertEqual(lc.classify_status(code), lc.DEAD, code)

    def test_403_429_503_are_blocked_not_dead(self):
        for code in (401, 403, 405, 429, 503, 999):
            self.assertEqual(lc.classify_status(code), lc.BLOCKED, code)

    def test_5xx_is_unknown(self):
        for code in (500, 502, 504):
            self.assertEqual(lc.classify_status(code), lc.UNKNOWN, code)

    def test_unfollowed_redirect_is_dead(self):
        self.assertEqual(lc.classify_status(302), lc.DEAD)

    def test_unknown_code_is_unknown(self):
        self.assertEqual(lc.classify_status(418), lc.UNKNOWN)


# ---------------------------------------------------------------------------
# Local files and anchors
# ---------------------------------------------------------------------------

class TestCheckLocal(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name
        self.md = os.path.join(self.root, "doc.md")
        with open(self.md, "w", encoding="utf-8") as fh:
            fh.write("# Install\n\n## Two Words\n")
        self.py = os.path.join(self.root, "code.py")
        with open(self.py, "w", encoding="utf-8") as fh:
            fh.write("a\nb\nc\n")

    def tearDown(self):
        self.dir.cleanup()

    def check(self, path, fragment=""):
        return lc.check_local(path, fragment, {})

    def test_existing_file_is_ok(self):
        self.assertEqual(self.check(self.md).kind, lc.OK)

    def test_missing_file_is_dead(self):
        verdict = self.check(os.path.join(self.root, "nope.md"))
        self.assertEqual(verdict.kind, lc.DEAD)
        self.assertIn("not found", verdict.reason)

    def test_existing_anchor_is_ok(self):
        self.assertEqual(self.check(self.md, "two-words").kind, lc.OK)

    def test_missing_anchor_is_dead(self):
        self.assertEqual(self.check(self.md, "nope").kind, lc.DEAD)

    def test_line_anchor_within_file(self):
        self.assertEqual(self.check(self.py, "L2").kind, lc.OK)

    def test_line_anchor_past_end_is_dead(self):
        self.assertEqual(self.check(self.py, "L99").kind, lc.DEAD)

    def test_fragment_in_binary_file_is_unknown(self):
        self.assertEqual(self.check(self.py, "anything").kind, lc.UNKNOWN)

    def test_directory_is_ok(self):
        self.assertEqual(self.check(self.root).kind, lc.OK)


# ---------------------------------------------------------------------------
# End to end against a live server
# ---------------------------------------------------------------------------

class TestRemote(ServerCase):
    def check(self, path, fragment="", timeout=5.0):
        return lc.check_remote(self.url(path), fragment, timeout, True)

    def test_200_is_ok(self):
        self.assertEqual(self.check("/ok").kind, lc.OK)

    def test_404_is_dead(self):
        self.assertEqual(self.check("/missing").kind, lc.DEAD)

    def test_410_is_dead(self):
        self.assertEqual(self.check("/gone").kind, lc.DEAD)

    def test_403_is_blocked(self):
        verdict = self.check("/forbidden")
        self.assertEqual(verdict.kind, lc.BLOCKED)
        self.assertEqual(verdict.code, 403)

    def test_429_is_blocked(self):
        self.assertEqual(self.check("/ratelimit").kind, lc.BLOCKED)

    def test_503_is_blocked(self):
        self.assertEqual(self.check("/unavailable").kind, lc.BLOCKED)

    def test_500_is_unknown(self):
        self.assertEqual(self.check("/servererror").kind, lc.UNKNOWN)

    def test_redirect_to_200_is_ok(self):
        self.assertEqual(self.check("/redirect").kind, lc.OK)

    def test_redirect_loop_is_dead(self):
        verdict = self.check("/loop")
        self.assertEqual(verdict.kind, lc.DEAD)
        self.assertIn("loop", verdict.reason)

    def test_head_405_falls_back_to_get(self):
        self.assertEqual(self.check("/head405").kind, lc.OK)

    def test_head_404_falls_back_to_get(self):
        self.assertEqual(self.check("/head404").kind, lc.OK)

    def test_existing_fragment(self):
        self.assertEqual(self.check("/page", "install").kind, lc.OK)

    def test_named_anchor_fragment(self):
        self.assertEqual(self.check("/page", "legacy").kind, lc.OK)

    def test_missing_fragment_is_dead(self):
        verdict = self.check("/page", "nope")
        self.assertEqual(verdict.kind, lc.DEAD)
        self.assertIn("#nope", verdict.reason)

    def test_fragment_on_pdf_is_unknown(self):
        self.assertEqual(self.check("/doc.pdf", "page=2").kind, lc.UNKNOWN)

    def test_line_anchor_on_web_page_is_unknown(self):
        self.assertEqual(self.check("/page", "L119").kind, lc.UNKNOWN)

    def test_timeout_is_unknown(self):
        verdict = self.check("/slow", timeout=0.4)
        self.assertEqual(verdict.kind, lc.UNKNOWN)
        self.assertIn("timed out", verdict.reason)

    def test_unresolvable_host_is_dead(self):
        verdict = lc.check_remote("http://no-such-host.invalid/x", "", 2.0, True)
        self.assertEqual(verdict.kind, lc.DEAD)

    def test_connection_refused_is_unknown(self):
        verdict = lc.check_remote("http://127.0.0.1:9/x", "", 2.0, True)
        self.assertEqual(verdict.kind, lc.UNKNOWN)

    def test_fragments_can_be_switched_off(self):
        verdict = lc.check_remote(self.url("/page"), "nope", 5.0, False)
        self.assertEqual(verdict.kind, lc.OK)

    def test_anchor_past_the_read_cap_is_unknown_not_dead(self):
        # Regression: a 2.8 MB manual whose #anchor sits at byte 1.4 MB was
        # reported as a dead link, because only the first 512 KB were read.
        verdict = self.check("/bigpage", "late")
        self.assertEqual(verdict.kind, lc.UNKNOWN)
        self.assertIn("not verified", verdict.reason)

    def test_anchor_inside_the_read_cap_is_still_ok(self):
        self.assertEqual(self.check("/bigpage-early", "early").kind, lc.OK)


# ---------------------------------------------------------------------------
# Whole runs
# ---------------------------------------------------------------------------

class TestRun(ServerCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name

    def tearDown(self):
        self.dir.cleanup()

    def write(self, name, body):
        path = os.path.join(self.root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def test_counts_and_exit_material(self):
        self.write("a.md", f"[ok]({self.url('/ok')})\n[dead]({self.url('/missing')})\n")
        report = lc.run([self.root])
        counts = report.counts()
        self.assertEqual(counts[lc.OK], 1)
        self.assertEqual(counts[lc.DEAD], 1)

    def test_duplicate_urls_are_checked_once(self):
        body = "".join(f"[x]({self.url('/ok')})\n" for _ in range(9))
        self.write("a.md", body)
        report = lc.run([self.root])
        self.assertEqual(report.links, 9)
        self.assertEqual(report.targets, 1)
        self.assertEqual(report.counts()[lc.OK], 9)

    def test_same_page_two_fragments_is_two_targets(self):
        self.write("a.md", f"[a]({self.url('/page')}#install)\n[b]({self.url('/page')}#usage)\n")
        report = lc.run([self.root])
        self.assertEqual(report.targets, 2)
        self.assertEqual(report.counts()[lc.OK], 2)

    def test_local_only_makes_no_network_calls(self):
        self.write("a.md", f"[ext]({self.url('/missing')})\n[loc](other.md)\n")
        self.write("other.md", "# hi\n")
        report = lc.run([self.root], only="local")
        self.assertEqual(report.targets, 1)
        self.assertEqual(report.counts()[lc.OK], 1)

    def test_external_only_skips_local(self):
        self.write("a.md", f"[ext]({self.url('/ok')})\n[loc](other.md)\n")
        self.write("other.md", "# hi\n")
        report = lc.run([self.root], only="external")
        self.assertEqual(report.targets, 1)

    def test_exclude_pattern(self):
        self.write("a.md", f"[x]({self.url('/missing')})\n")
        report = lc.run([self.root], exclude=re.compile(r"/missing"))
        self.assertEqual(report.targets, 0)

    def test_self_anchor_resolves_to_own_file(self):
        self.write("a.md", "# Real Heading\n\n[jump](#real-heading)\n")
        report = lc.run([self.root])
        self.assertEqual(report.counts()[lc.OK], 1)

    def test_missing_self_anchor_is_dead(self):
        self.write("a.md", "# Real Heading\n\n[jump](#gone)\n")
        report = lc.run([self.root])
        self.assertEqual(report.counts()[lc.DEAD], 1)

    def test_unsupported_scheme_is_skipped(self):
        self.write("a.md", "[m](mailto:a@b.c)\n[t](tel:+123)\n")
        report = lc.run([self.root])
        self.assertEqual(report.counts()[lc.SKIPPED], 2)

    def test_deadline_marks_leftovers_skipped(self):
        body = "".join(f"[x]({self.url('/slow')}?i={i})\n" for i in range(4))
        self.write("a.md", body)
        report = lc.run([self.root], timeout=5.0, jobs=1, deadline=0.2)
        self.assertGreater(report.skipped_budget, 0)

    def test_text_output_has_one_row_per_broken_target(self):
        body = "".join(f"[x]({self.url('/missing')})\n" for _ in range(5))
        self.write("a.md", body)
        report = lc.run([self.root])
        text = lc.render_text(report)
        self.assertEqual(text.count("404 Not Found"), 1)
        self.assertIn("(+2 more)", text)

    def test_json_is_parseable_and_complete(self):
        self.write("a.md", f"[ok]({self.url('/ok')})\n[dead]({self.url('/missing')})\n")
        report = lc.run([self.root])
        payload = json.loads(lc.render_json(report))
        self.assertEqual(payload["links"], 2)
        self.assertEqual(len(payload["results"]), 2)
        self.assertEqual(payload["counts"][lc.DEAD], 1)


class RootlessHandler(BaseHTTPRequestHandler):
    """A host that answers 404 to everything, root included - bot protection."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_HEAD(self):
        self._send()

    def do_GET(self):
        self._send()

    def _send(self):
        body = b"not found"
        self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


class TestSuspectHost(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), RootlessHandler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_404_from_a_host_that_404s_its_root_is_blocked(self):
        # Regression: crates.io answers 404 to every HTML page for clients it
        # does not like, which turned one typo'd crate into a "dead link".
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "a.md")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(f"[crate](http://127.0.0.1:{self.port}/crates/serde)\n")
            report = lc.run([root], timeout=5.0)
            verdict = report.results[report.order[0]]
            self.assertEqual(verdict.kind, lc.BLOCKED)
            self.assertIn("own root", verdict.reason)
            self.assertEqual(report.downgraded, 1)
            self.assertEqual(report.counts()[lc.DEAD], 0)


class TestCli(ServerCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = self.dir.name

    def tearDown(self):
        self.dir.cleanup()

    def run_cli(self, argv):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = lc.main(argv)
        return code, buffer.getvalue()

    def test_exit_zero_when_all_ok(self):
        path = os.path.join(self.root, "a.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"[x]({self.url('/ok')})\n")
        code, out = self.run_cli([self.root])
        self.assertEqual(code, 0)
        self.assertIn("nothing dead", out)

    def test_exit_one_when_dead(self):
        path = os.path.join(self.root, "a.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"[x]({self.url('/missing')})\n")
        code, out = self.run_cli([self.root])
        self.assertEqual(code, 1)
        self.assertIn("dead", out)

    def test_blocked_alone_does_not_fail(self):
        path = os.path.join(self.root, "a.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"[x]({self.url('/forbidden')})\n")
        code, _out = self.run_cli([self.root])
        self.assertEqual(code, 0)

    def test_strict_fails_on_blocked(self):
        path = os.path.join(self.root, "a.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"[x]({self.url('/forbidden')})\n")
        code, _out = self.run_cli([self.root, "--strict"])
        self.assertEqual(code, 1)

    def test_json_flag(self):
        path = os.path.join(self.root, "a.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"[x]({self.url('/ok')})\n")
        code, out = self.run_cli([self.root, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["links"], 1)

    def test_no_fragments_flag(self):
        path = os.path.join(self.root, "a.md")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"[x]({self.url('/page')}#nope)\n")
        code, _out = self.run_cli([self.root, "--no-fragments"])
        self.assertEqual(code, 0)

    def test_bad_exclude_regex_exits_two(self):
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli([self.root, "--exclude", "("])
        self.assertEqual(ctx.exception.code, 2)

    def test_missing_input_exits_with_message(self):
        with self.assertRaises(SystemExit):
            lc.collect_documents([os.path.join(self.root, "nope")])


if __name__ == "__main__":
    unittest.main()
