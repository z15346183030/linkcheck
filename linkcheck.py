#!/usr/bin/env python3
"""linkcheck - check the links in your docs, and fail only on the ones that are really broken.

One file, standard library only, no config file.

The point of a link checker is not to print "this link failed". It is to answer
one question: *is this link broken for a reader?* Those are not the same thing.
A 403 from Cloudflare, a 429 from a rate limiter, a 503 during a deploy, a
timeout on a train - none of them mean the page is gone, and all of them turn a
checker's output into noise. A checker that cries wolf gets switched off.

So every link gets one of five verdicts:

    ok       2xx (after redirects), or a local file that exists
    dead     404 / 410 / 451 / 400 / 414, a redirect loop, a host that does not
             resolve, a missing local file, a missing #anchor
    blocked  401 / 403 / 405 / 407 / 429 / 503 / 999 - the server refused this
             client, which says nothing about the page
    unknown  timeout, TLS error, connection refused, 5xx - could be permanent,
             could be a bad minute
    skipped  mailto:, tel:, javascript:, data:, an unsupported scheme, or a link
             that ran out of the time budget

Only `dead` sets the exit code. `--strict` also fails on `blocked` and `unknown`
when you want the paranoid version.

Usage:
    python linkcheck.py docs/
    python linkcheck.py README.md --only local
    python linkcheck.py . --json > links.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from html.parser import HTMLParser

VERSION = "1.0.0"
USER_AGENT = "linkcheck/1.0 (documentation link checker)"

# ---------------------------------------------------------------------------
# Verdicts
# ---------------------------------------------------------------------------

OK = "ok"
DEAD = "dead"
BLOCKED = "blocked"
UNKNOWN = "unknown"
SKIPPED = "skipped"

SEVERITY = {DEAD: 0, BLOCKED: 1, UNKNOWN: 2, SKIPPED: 3, OK: 4}

# 404 and friends: the resource is gone, full stop.
DEAD_STATUS = frozenset({400, 404, 410, 414, 451})
# The server answered, and the answer is "not to you". A browser with a cookie
# and a human in front of it would very likely get a 200.
BLOCKED_STATUS = frozenset({401, 403, 405, 406, 407, 429, 503, 999})
# Everything else that is not 2xx/3xx lands in `unknown`: 408, 425, 500, 502,
# 504, and any status this tool has never heard of.

MAX_REDIRECTS = 10
# Per-host parallelism. One would be politer, but a single unresponsive host
# would then decide the run time for every other link. Four keeps a doc tree
# from hammering a server while staying out of its own way.
PER_HOST = 4
MAX_BODY_BYTES = 512 * 1024
DOC_SUFFIXES = (".md", ".markdown", ".mdown", ".html", ".htm")
SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
     "env", "site-packages", ".mypy_cache", ".pytest_cache", "dist", "build",
     ".tox", ".idea", ".vscode"}
)
SKIP_SCHEMES = frozenset({"mailto", "tel", "sms", "javascript", "data", "irc",
                          "magnet", "slack", "zoommtg", "file"})
LINE_ANCHOR = re.compile(r"^L(\d+)$")


@dataclass
class Verdict:
    kind: str
    code: int | None = None
    reason: str = ""
    final_url: str = ""

    @property
    def label(self) -> str:
        if self.code is not None:
            return f"{self.code} {self.reason}".strip()
        return self.reason


@dataclass(frozen=True)
class Found:
    """One link as written in one document."""

    target: str
    doc: str
    line: int
    col: int


@dataclass
class Target:
    """One distinct thing that has to be checked, plus everywhere it is used."""

    raw: str
    kind: str                      # "local" or "remote"
    key: str                       # absolute path, or URL without the fragment
    fragment: str
    base_dir: str
    sources: list[Found] = field(default_factory=list)


@dataclass
class Report:
    files: int = 0
    links: int = 0
    targets: int = 0
    seconds: float = 0.0
    results: dict[str, Verdict] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    target_list: list[Target] = field(default_factory=list)
    skipped_budget: int = 0
    downgraded: int = 0
    by_doc: dict[str, list[tuple[Found, Verdict]]] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        tally = {OK: 0, DEAD: 0, BLOCKED: 0, UNKNOWN: 0, SKIPPED: 0}
        for found_list in self.by_doc.values():
            for _found, verdict in found_list:
                tally[verdict.kind] = tally.get(verdict.kind, 0) + 1
        return tally


# ---------------------------------------------------------------------------
# Classification - the whole argument of the tool, in one pure function
# ---------------------------------------------------------------------------

def classify_status(code: int) -> str:
    """Map an HTTP status code to a verdict kind."""
    if 200 <= code < 300:
        return OK
    if code in DEAD_STATUS:
        return DEAD
    if code in BLOCKED_STATUS:
        return BLOCKED
    if 300 <= code < 400:
        # A redirect we did not follow (too many, or a loop) is a dead end.
        return DEAD
    return UNKNOWN


def classify_error(exc: BaseException) -> tuple[str, str]:
    """Map a transport error to (kind, human reason)."""
    reason = getattr(exc, "reason", exc)
    text = str(reason)
    if isinstance(reason, socket.gaierror) or "getaddrinfo" in text or "Name or service not known" in text:
        return DEAD, "host does not resolve"
    if isinstance(reason, ConnectionRefusedError) or "refused" in text.lower():
        return UNKNOWN, "connection refused"
    if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in text.lower():
        return UNKNOWN, "timed out"
    if isinstance(reason, ssl.SSLError) or "certificate" in text.lower() or "SSL" in text:
        return UNKNOWN, "TLS handshake failed"
    if "reset" in text.lower() or "aborted" in text.lower() or "closed" in text.lower():
        return UNKNOWN, "connection reset"
    return UNKNOWN, text.split("\n")[0][:120] or exc.__class__.__name__


# ---------------------------------------------------------------------------
# Anchors
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
_ATX = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HTML_ID = re.compile(r"""\b(?:id|name)\s*=\s*["']([^"']+)["']""", re.I)
_IMAGE_OR_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_HTML_TAG = re.compile(r"<[^>]+>")


def slugify(text: str) -> str:
    """GitHub's heading slug: lowercase, drop punctuation, spaces become '-'."""
    text = text.strip().lower()
    text = _IMAGE_OR_LINK.sub(r"\1", text)
    text = _HTML_TAG.sub("", text)
    text = text.replace("`", "")
    out = []
    for ch in text:
        if ch.isalnum() or ch in "-_":
            out.append(ch)
        elif ch in " \t":
            out.append("-")
        # everything else (punctuation, emoji) is dropped, like GitHub does
    return "".join(out)


def _fenced_lines(text: str) -> list[bool]:
    """True for every line that sits inside (or on) a fenced code block."""
    flags = []
    fence = None
    for line in text.splitlines():
        m = _FENCE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)[0] * 3
                flags.append(True)
            else:
                flags.append(False)
        else:
            flags.append(True)
            if m and m.group(1).startswith(fence):
                fence = None
    return flags


def anchors_in_markdown(text: str) -> set[str]:
    """Every #fragment a Markdown file offers: headings, plus explicit HTML ids."""
    lines = text.splitlines()
    fenced = _fenced_lines(text)
    raw: list[str] = []
    i = 0
    # Skip a YAML front matter block so `title: x` + `---` is not read as a heading.
    if lines and lines[0].strip() == "---":
        for j in range(1, len(lines)):
            if lines[j].strip() in ("---", "..."):
                i = j + 1
                break
    while i < len(lines):
        line = lines[i]
        if fenced[i]:
            i += 1
            continue
        m = _ATX.match(line)
        if m:
            raw.append(slugify(m.group(2)))
            i += 1
            continue
        # setext: the underline belongs to the line above it
        if i + 1 < len(lines) and not fenced[i + 1] and line.strip():
            under = lines[i + 1].strip()
            if re.fullmatch(r"={3,}", under) or re.fullmatch(r"-{3,}", under):
                if not line.lstrip().startswith(("#", ">", "|", "-", "*", "+")):
                    raw.append(slugify(line))
                    i += 2
                    continue
        for hit in _HTML_ID.finditer(line):
            raw.append(hit.group(1).lower())
        i += 1
    return _dedupe_slugs(raw)


def _dedupe_slugs(raw: list[str]) -> set[str]:
    """GitHub appends -1, -2 ... to repeated headings. Mirror that."""
    seen: dict[str, int] = {}
    out: set[str] = set()
    for slug in raw:
        if not slug:
            continue
        if slug in out:
            n = seen.get(slug, 0)
            while f"{slug}-{n + 1}" in out:
                n += 1
            seen[slug] = n + 1
            out.add(f"{slug}-{n + 1}")
        else:
            out.add(slug)
    return out


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: set[str] = set()

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if not value:
                continue
            if name == "id":
                self.anchors.add(value.lower())
            elif name == "name" and tag == "a":
                self.anchors.add(value.lower())


def anchors_in_html(text: str) -> set[str]:
    parser = _AnchorParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        pass
    return parser.anchors


def _normalise_fragment(fragment: str) -> str:
    frag = fragment.lower()
    if frag.startswith("user-content-"):
        frag = frag[len("user-content-"):]
    return frag


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def _strip_code_spans(line: str) -> str:
    """Blank out `inline code` so links inside it are not links. Columns survive."""
    out = list(line)
    i = 0
    while i < len(line):
        if line[i] != "`":
            i += 1
            continue
        j = i
        while j < len(line) and line[j] == "`":
            j += 1
        ticks = line[i:j]
        close = line.find(ticks, j)
        if close == -1:
            break
        for p in range(i, close + len(ticks)):
            out[p] = " "
        i = close + len(ticks)
    return "".join(out)


def _paren_target(line: str, start: int) -> tuple[str, int] | None:
    """Read the (target) part of a Markdown link starting at `start` (after '(')."""
    depth = 0
    i = start
    in_angle = False
    while i < len(line):
        ch = line[i]
        if ch == "<" and i == start:
            in_angle = True
        elif ch == ">" and in_angle:
            in_angle = False
        elif ch == "(" and not in_angle:
            depth += 1
        elif ch == ")" and not in_angle:
            if depth == 0:
                return line[start:i], i
            depth -= 1
        i += 1
    return None


def _clean_target(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("<"):
        end = raw.find(">")
        return raw[1:end] if end > 0 else raw[1:]
    # drop a trailing "title" or 'title'
    m = re.match(r"""^(\S+)\s+(?:"[^"]*"|'[^']*'|\([^)]*\))\s*$""", raw)
    if m:
        return m.group(1)
    return raw.split()[0] if raw.split() else ""


_REF_DEF = re.compile(r"^\s{0,3}\[([^\]]+)\]:\s*(\S+)")
_AUTOLINK = re.compile(r"<((?:https?|ftp)://[^>\s]+)>")
_ATTR = re.compile(r"""\b(?:href|src)\s*=\s*(?:"([^"]*)"|'([^']*)')""", re.I)


def extract_markdown(text: str, doc: str) -> list[Found]:
    """All link targets in a Markdown document, with line and column."""
    found: list[Found] = []
    fenced = _fenced_lines(text)
    for lineno, line in enumerate(text.splitlines(), start=1):
        if fenced[lineno - 1]:
            continue
        clean = _strip_code_spans(line)
        # [text](target) and ![alt](target)
        i = 0
        while True:
            marker = clean.find("](", i)
            if marker == -1:
                break
            # find the '[' that opens this label
            depth = 0
            open_at = -1
            for p in range(marker, -1, -1):
                if clean[p] == "]":
                    depth += 1
                elif clean[p] == "[":
                    depth -= 1
                    if depth == 0:
                        open_at = p
                        break
            i = marker + 2
            if open_at == -1:
                continue
            parsed = _paren_target(clean, marker + 2)
            if parsed is None:
                continue
            target = _clean_target(parsed[0])
            if target:
                found.append(Found(target, doc, lineno, open_at + 1))
        # [id]: target
        m = _REF_DEF.match(clean)
        if m:
            found.append(Found(_clean_target(m.group(2)), doc, lineno, m.start(2) + 1))
        # <https://autolink>
        for m in _AUTOLINK.finditer(clean):
            found.append(Found(m.group(1), doc, lineno, m.start(1) + 1))
        # inline HTML inside Markdown
        for m in _ATTR.finditer(clean):
            target = m.group(1) if m.group(1) is not None else m.group(2)
            if target:
                found.append(Found(target, doc, lineno, m.start() + 1))
    return found


class _LinkParser(HTMLParser):
    def __init__(self, doc: str) -> None:
        super().__init__(convert_charrefs=True)
        self.doc = doc
        self.found: list[Found] = []

    def handle_starttag(self, tag, attrs):
        line, _col = self.getpos()
        for name, value in attrs:
            if name in ("href", "src") and value:
                self.found.append(Found(value, self.doc, line, 1))


def extract_html(text: str, doc: str) -> list[Found]:
    parser = _LinkParser(doc)
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        pass
    return parser.found


def extract(text: str, doc: str) -> list[Found]:
    if doc.lower().endswith((".html", ".htm")):
        return extract_html(text, doc)
    return extract_markdown(text, doc)


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------

def _looks_remote(target: str) -> bool:
    return bool(re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target)) and not target.startswith("file:")


def resolve_local(target: str, base_dir: str) -> str:
    """Turn a relative/absolute/tilde/percent-encoded target into a filesystem path."""
    path = urllib.parse.unquote(target.split("?")[0])
    if path.startswith("~"):
        path = os.path.expanduser(path)
    if not os.path.isabs(path):
        path = os.path.join(base_dir, path)
    return os.path.normpath(path)


def _split_fragment(target: str) -> tuple[str, str]:
    if "#" not in target:
        return target, ""
    base, frag = target.split("#", 1)
    return base, frag


# ---------------------------------------------------------------------------
# Local checking
# ---------------------------------------------------------------------------

def _count_lines(path: str) -> int:
    try:
        with open(path, "rb") as fh:
            return fh.read().count(b"\n") + 1
    except OSError:
        return 0


def check_local(path: str, fragment: str, anchors_cache: dict[str, set[str] | None]) -> Verdict:
    if not os.path.exists(path):
        return Verdict(DEAD, None, "file not found")
    if not fragment or fragment in ("top", "#"):
        return Verdict(OK)
    frag = _normalise_fragment(fragment)

    m = LINE_ANCHOR.match(fragment)
    if m:
        want = int(m.group(1))
        have = _count_lines(path)
        if os.path.isdir(path):
            return Verdict(UNKNOWN, None, "line anchor on a directory")
        if want <= have:
            return Verdict(OK)
        return Verdict(DEAD, None, f"line anchor L{want} past end of file ({have} lines)")

    if os.path.isdir(path):
        return Verdict(UNKNOWN, None, "fragment on a directory")

    lower = path.lower()
    if not lower.endswith(DOC_SUFFIXES):
        return Verdict(UNKNOWN, None, "fragment in a file this tool cannot parse")

    if path not in anchors_cache:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError:
            anchors_cache[path] = None
        else:
            anchors_cache[path] = anchors_in_html(body) if lower.endswith((".html", ".htm")) else anchors_in_markdown(body)
    anchors = anchors_cache[path]
    if anchors is None:
        return Verdict(UNKNOWN, None, "file could not be read")
    if frag in anchors:
        return Verdict(OK)
    return Verdict(DEAD, None, f"no #{fragment} in {os.path.basename(path)}")


# ---------------------------------------------------------------------------
# Remote checking
# ---------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects by hand, so loops can be named and chains can be reported."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request_once(url: str, method: str, timeout: float, want_body: bool) -> tuple[int, str, bytes, dict, bool]:
    req = urllib.request.Request(url, method=method)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "*/*")
    req.add_header("Accept-Encoding", "identity")
    req.add_header("Connection", "close")
    resp = _OPENER.open(req, timeout=timeout)
    try:
        code = resp.getcode()
        headers = dict(resp.headers.items())
        body = b""
        truncated = False
        if want_body and method == "GET":
            # Read one byte past the cap: that byte is how we know the page was
            # cut off, which decides whether a missing anchor may be called dead.
            body = resp.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                body = body[:MAX_BODY_BYTES]
                truncated = True
        return code, resp.geturl(), body, headers, truncated
    finally:
        resp.close()


def _fetch(url: str, timeout: float, want_body: bool) -> tuple[int | None, str, bytes, str, str, str, bool]:
    """Fetch a URL, following redirects by hand.

    Returns (code, final_url, body, content_type, error_kind, error_text, truncated).
    HEAD first when the body is not needed - it is the polite method - then GET,
    because a surprising number of servers answer HEAD with 403/404/405 and GET
    with 200. When the body *is* needed (fragment check) we start with GET.
    """
    method = "GET" if want_body else "HEAD"
    code = None
    final = url
    body = b""
    ctype = ""
    truncated = False
    visited = [url]
    hops = 0
    while True:
        try:
            code, final, body, headers, truncated = _request_once(final, method, timeout, want_body)
            ctype = headers.get("Content-Type", "") or headers.get("content-type", "")
        except urllib.error.HTTPError as exc:
            code = exc.code
            final = exc.geturl()
            if code in (301, 302, 303, 307, 308):
                location = exc.headers.get("location") or exc.headers.get("Location")
                if not location:
                    return code, final, b"", "", "", "", False
                nxt = urllib.parse.urljoin(final, location)
                if nxt in visited:
                    return None, nxt, b"", "", "loop", "redirect loop", False
                if hops >= MAX_REDIRECTS:
                    return None, nxt, b"", "", "loop", f"more than {MAX_REDIRECTS} redirects", False
                visited.append(nxt)
                hops += 1
                final = nxt
                continue
            body = b""
        except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
            kind, text = classify_error(exc)
            return None, final, b"", "", kind, text, False
        # A 3xx that urllib handed back instead of raising (rare, but cheap to handle)
        if code in (301, 302, 303, 307, 308):
            return code, final, b"", ctype, "", "", False
        if code >= 400 and method == "HEAD":
            # One extra request buys a verdict that is about the page, not the method.
            method = "GET"
            visited = [final]
            hops = 0
            continue
        return code, final, body, ctype, "", "", truncated


def _page_anchors(body: bytes, content_type: str) -> set[str] | None:
    ctype = (content_type or "").lower()
    if ctype and not any(t in ctype for t in ("html", "text/plain", "markdown")):
        return None
    text = body.decode("utf-8", errors="replace")
    return anchors_in_html(text)


def check_remote(url: str, fragment: str, timeout: float, check_fragments: bool) -> Verdict:
    want_body = bool(fragment) and check_fragments
    code, final, body, ctype, err_kind, err_text, truncated = _fetch(url, timeout, want_body)
    if code is None:
        # A redirect loop or a redirect chain past the limit is a dead end:
        # the reader can never arrive.
        kind = DEAD if err_kind == "loop" else (err_kind or UNKNOWN)
        return Verdict(kind, None, err_text, final)

    kind = classify_status(code)
    if kind != OK:
        return Verdict(kind, code, _status_reason(code), final)
    if not want_body or not fragment or fragment == "top":
        return Verdict(OK, code, "", final)

    if LINE_ANCHOR.match(fragment):
        # "#L119" on a code host is drawn by JavaScript, not present in the HTML.
        # Reporting it as broken would be a guess; say so instead.
        return Verdict(UNKNOWN, code, "line anchor cannot be verified on a web page", final)

    anchors = _page_anchors(body, ctype)
    if anchors is None:
        return Verdict(UNKNOWN, code, "fragment target is not HTML", final)
    if _normalise_fragment(fragment) in anchors:
        return Verdict(OK, code, "", final)
    if truncated:
        # The page is bigger than we were willing to read, so "not found" here
        # means "not in the first 512 KB". That is not proof of anything.
        return Verdict(UNKNOWN, code,
                       f"page larger than {MAX_BODY_BYTES // 1024} KB, #{fragment} not verified", final)
    return Verdict(DEAD, None, f"page has no #{fragment} (HTTP {code})", final)


_REASONS = {
    400: "Bad Request", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 406: "Not Acceptable", 407: "Proxy Auth Required",
    408: "Request Timeout", 410: "Gone", 414: "URI Too Long", 418: "I'm a teapot",
    425: "Too Early", 429: "Too Many Requests", 451: "Unavailable For Legal Reasons",
    500: "Internal Server Error", 502: "Bad Gateway", 503: "Service Unavailable",
    504: "Gateway Timeout", 999: "LinkedIn: blocked",
}


def _status_reason(code: int) -> str:
    return _REASONS.get(code, "")


def _host_root(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}/"


def downgrade_suspect_hosts(report: Report, targets: list[Target], timeout: float) -> int:
    """A 404 only means "gone" if the host still answers 200 about something else.

    Some sites (crates.io among them) answer 404 to every HTML page for clients
    they do not like, root included. On those hosts a 404 is evidence of a
    refusal, not of a missing page, so the verdict is downgraded to `blocked`
    after asking the host about its own front door. Costs one request per host
    that produced a 404, and it is the difference between "12 dead links" and
    "this site does not want to talk to you".
    """
    suspects: dict[str, list[str]] = {}
    for target in targets:
        if target.kind != "remote":
            continue
        key = target.key + "\0" + target.fragment
        verdict = report.results.get(key)
        if verdict and verdict.kind == DEAD and verdict.code in (404, 410):
            suspects.setdefault(_host_root(target.key), []).append(key)
    if not suspects:
        return 0
    changed = 0
    for root, keys in suspects.items():
        probe = check_remote(root, "", timeout, False)
        if probe.code not in (404, 410):
            continue
        for key in keys:
            old = report.results[key]
            report.results[key] = Verdict(
                BLOCKED, old.code,
                f"host returns {probe.code} for its own root - refusing this client, not a missing page",
                old.final_url,
            )
            changed += 1
    return changed


# ---------------------------------------------------------------------------
# Collecting documents
# ---------------------------------------------------------------------------

def collect_documents(paths: list[str]) -> list[str]:
    docs: list[str] = []
    for given in paths:
        if os.path.isdir(given):
            for root, dirs, files in os.walk(given):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
                for name in sorted(files):
                    if name.lower().endswith(DOC_SUFFIXES):
                        docs.append(os.path.join(root, name))
        elif os.path.isfile(given):
            docs.append(given)
        else:
            raise SystemExit(f"linkcheck: no such file or directory: {given}")
    # keep order, drop duplicates
    seen = set()
    out = []
    for doc in docs:
        key = os.path.normcase(os.path.abspath(doc))
        if key not in seen:
            seen.add(key)
            out.append(doc)
    return out


def build_targets(docs: list[str], only: str, exclude: re.Pattern | None) -> tuple[list[Target], int]:
    """Read every document, extract links, and fold duplicates into single targets."""
    targets: dict[str, Target] = {}
    order: list[str] = []
    total = 0
    for doc in docs:
        try:
            with open(doc, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError as exc:
            raise SystemExit(f"linkcheck: cannot read {doc}: {exc}")
        base_dir = os.path.dirname(os.path.abspath(doc))
        for found in extract(text, doc):
            raw = found.target.strip()
            if not raw:
                continue
            if exclude and exclude.search(raw):
                continue
            scheme = raw.split(":", 1)[0].lower() if _looks_remote(raw) else ""
            if scheme in SKIP_SCHEMES:
                key = "skip:" + raw
                if key not in targets:
                    targets[key] = Target(raw, "skipped", key, "", base_dir)
                    order.append(key)
                targets[key].sources.append(found)
                total += 1
                continue
            if _looks_remote(raw):
                if only == "local":
                    continue
                base, frag = _split_fragment(raw)
                key = base + "\0" + frag
                if key not in targets:
                    targets[key] = Target(raw, "remote", base, frag, base_dir)
                    order.append(key)
                targets[key].sources.append(found)
                total += 1
                continue
            if only == "external":
                continue
            base, frag = _split_fragment(raw)
            # "#section" with no path means "a heading in this very document".
            if not base and frag:
                path = os.path.abspath(doc)
            else:
                path = resolve_local(base, base_dir)
            key = os.path.normcase(path) + "\0" + frag
            if key not in targets:
                targets[key] = Target(raw, "local", path, frag, base_dir)
                order.append(key)
            targets[key].sources.append(found)
            total += 1
    return [targets[k] for k in order], total


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

def run(
    paths: list[str],
    only: str = "all",
    timeout: float = 10.0,
    jobs: int = 8,
    deadline: float | None = None,
    fragments: bool = True,
    exclude: re.Pattern | None = None,
) -> Report:
    started = time.monotonic()
    docs = collect_documents(paths)
    targets, total = build_targets(docs, only, exclude)
    report = Report(files=len(docs), links=total, targets=len(targets))
    if not targets:
        report.seconds = time.monotonic() - started
        report.by_doc = {doc: [] for doc in docs}
        return report

    host_sems: dict[str, threading.Semaphore] = {}
    lock_guard = threading.Lock()
    anchors_cache: dict[str, set[str] | None] = {}
    budget_hit = threading.Event()

    def host_sem(url: str) -> threading.Semaphore:
        host = urllib.parse.urlsplit(url).netloc
        with lock_guard:
            if host not in host_sems:
                host_sems[host] = threading.Semaphore(PER_HOST)
            return host_sems[host]

    def work(target: Target) -> tuple[str, Verdict]:
        key = target.key + "\0" + target.fragment
        if target.kind == "skipped":
            return key, Verdict(SKIPPED, None, "not a checkable link")
        if deadline is not None and time.monotonic() - started > deadline:
            budget_hit.set()
            return key, Verdict(SKIPPED, None, "time budget exhausted")
        if target.kind == "local":
            return key, check_local(target.key, target.fragment, anchors_cache)
        # At most PER_HOST requests in flight per host: polite to the server,
        # and one dead host cannot swallow the whole thread pool.
        with host_sem(target.key):
            return key, check_remote(target.key, target.fragment, timeout, fragments)

    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for key, verdict in pool.map(work, targets):
            report.results[key] = verdict
            report.order.append(key)

    # Second pass: a 404 is only a dead link if the host is still willing to
    # serve its own root. See downgrade_suspect_hosts.
    report.downgraded = downgrade_suspect_hosts(report, targets, timeout)

    by_doc: dict[str, list[tuple[Found, Verdict]]] = {doc: [] for doc in docs}
    for target in targets:
        key = target.key + "\0" + target.fragment
        verdict = report.results.get(key, Verdict(SKIPPED, None, "not checked"))
        for found in target.sources:
            by_doc.setdefault(found.doc, []).append((found, verdict))
    for doc in by_doc:
        by_doc[doc].sort(key=lambda pair: (SEVERITY[pair[1].kind], pair[0].line))
    report.by_doc = by_doc
    report.target_list = targets
    report.skipped_budget = sum(1 for v in report.results.values() if v.reason == "time budget exhausted")
    report.seconds = time.monotonic() - started
    return report


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _shorten(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "~"


def _display(path: str) -> str:
    """Show paths relative to where the command was run, with forward slashes."""
    try:
        rel = os.path.relpath(path)
    except ValueError:
        rel = path
    if rel.startswith(".."):
        rel = path
    return rel.replace(os.sep, "/")


def _places(sources: list[Found], per_doc: int = 3) -> str:
    """'docs/a.md:3, docs/b.md:12,14 (+7 more)' - one line, every location."""
    grouped: dict[str, list[int]] = {}
    for found in sources:
        grouped.setdefault(_display(found.doc), []).append(found.line)
    parts = []
    hidden = 0
    for doc, lines in grouped.items():
        lines = sorted(lines)
        head = ",".join(str(n) for n in lines[:per_doc])
        if len(lines) > per_doc:
            hidden += len(lines) - per_doc
        parts.append(f"{doc}:{head}")
    text = ", ".join(parts)
    if hidden:
        text += f" (+{hidden} more)"
    return text


def render_text(report: Report, strict: bool = False) -> str:
    lines: list[str] = []
    rows = []
    for target in report.target_list:
        verdict = report.results.get(target.key + "\0" + target.fragment)
        if verdict is None or verdict.kind == OK:
            continue
        first = min((f.line for f in target.sources), default=0)
        first_doc = target.sources[0].doc if target.sources else ""
        rows.append(((SEVERITY[verdict.kind], first_doc, first), target, verdict))
    rows.sort(key=lambda row: row[0])
    for _sort, target, verdict in rows:
        detail = verdict.label or verdict.kind
        lines.append(
            f"  {verdict.kind:<8} {_shorten(detail, 32):<32} {_shorten(target.raw, 56):<56} "
            f"{_places(target.sources)}"
        )
    counts = report.counts()
    distinct: dict[str, int] = {}
    for target in report.target_list:
        verdict = report.results.get(target.key + "\0" + target.fragment)
        if verdict is not None:
            distinct[verdict.kind] = distinct.get(verdict.kind, 0) + 1
    if rows:
        lines.append("")
    head = (f"linkcheck {VERSION} - {report.files} files, {report.links} links "
            f"({report.targets} distinct), {report.seconds:.2f}s")
    lines.append(head)
    summary = "  ".join(f"{k} {counts[k]}" for k in (OK, DEAD, BLOCKED, UNKNOWN, SKIPPED) if counts[k])
    lines.append(summary)
    if counts[DEAD]:
        lines.append(f"{counts[DEAD]} dead link{'s' if counts[DEAD] != 1 else ''} "
                     f"({distinct.get(DEAD, 0)} unique) - exit 1")
    elif strict and (counts[BLOCKED] or counts[UNKNOWN]):
        lines.append("nothing dead, but --strict counts blocked/unknown as failures - exit 1")
    else:
        lines.append("nothing dead")
    if counts[BLOCKED] and not strict:
        lines.append("blocked links were refused by the server, not by the reader; --strict fails on them too")
    if report.skipped_budget:
        lines.append(f"{report.skipped_budget} targets were never checked (--deadline reached)")
    return "\n".join(lines).lstrip("\n")


def render_json(report: Report) -> str:
    records = []
    for doc, pairs in report.by_doc.items():
        for found, verdict in pairs:
            records.append({
                "target": found.target,
                "doc": _display(found.doc),
                "line": found.line,
                "verdict": verdict.kind,
                "code": verdict.code,
                "reason": verdict.reason,
                "final_url": verdict.final_url or None,
            })
    payload = {
        "version": VERSION,
        "files": report.files,
        "links": report.links,
        "targets": report.targets,
        "seconds": round(report.seconds, 3),
        "counts": report.counts(),
        "results": records,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="linkcheck",
        description="Check links in Markdown and HTML, and only fail on the ones that are really broken.",
        epilog="Exit codes: 0 nothing dead, 1 dead links found (or blocked/unknown with --strict), 2 usage error.",
    )
    parser.add_argument("paths", nargs="*", default=["."], help="files or directories (default: .)")
    parser.add_argument("--only", choices=["all", "local", "external"], default="all",
                        help="check all links (default), only local ones (no network at all), or only remote ones")
    parser.add_argument("--timeout", type=float, default=10.0, metavar="SECONDS",
                        help="per-request timeout (default: 10)")
    parser.add_argument("--jobs", type=int, default=8, metavar="N",
                        help="parallel checks, one request at a time per host (default: 8)")
    parser.add_argument("--deadline", type=float, default=None, metavar="SECONDS",
                        help="stop after this many seconds and report what is left as skipped")
    parser.add_argument("--no-fragments", action="store_true",
                        help="do not verify #anchors (faster, but renamed headings go unnoticed)")
    parser.add_argument("--exclude", action="append", default=[], metavar="REGEX",
                        help="skip links matching this regular expression (repeatable)")
    parser.add_argument("--strict", action="store_true",
                        help="also exit 1 on blocked and unknown links")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--version", action="version", version=f"linkcheck {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    exclude = None
    if args.exclude:
        try:
            exclude = re.compile("|".join(f"(?:{p})" for p in args.exclude))
        except re.error as exc:
            parser.error(f"--exclude is not a valid regular expression: {exc}")
    report = run(
        args.paths or ["."],
        only=args.only,
        timeout=args.timeout,
        jobs=args.jobs,
        deadline=args.deadline,
        fragments=not args.no_fragments,
        exclude=exclude,
    )
    if args.json:
        print(render_json(report))
    else:
        print(render_text(report, strict=args.strict))
    counts = report.counts()
    if counts[DEAD]:
        return 1
    if args.strict and (counts[BLOCKED] or counts[UNKNOWN]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
