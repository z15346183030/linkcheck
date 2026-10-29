# linkcheck

**Check the links in your docs, and fail only on the ones that are really broken. One file, no dependencies, no config file.**

```bash
python linkcheck.py examples --only local
```

```
  dead     file not found                   troubleshooting.md          examples/docs/index.md:7
  dead     no #setup in guide.md            guide.md#setup              examples/docs/index.md:8
  dead     file not found                   CHANGELOG.md                examples/docs/index.md:10
  skipped  not a checkable link             mailto:docs@example.com     examples/docs/index.md:11

linkcheck 1.0.0 - 3 files, 10 links (10 distinct), 0.01s
ok 6  dead 3  skipped 1
3 dead links (3 unique) - exit 1
```

Three broken links in a three-page doc tree, and every one of them is real:
two files that were deleted, and an anchor `#setup` that died when the heading
was renamed to `## Install`. The `[not a link](nowhere.md)` inside a code span
and the one inside a fenced block are not links, and are not reported.

## Why this exists

`lychee` is the link checker most projects use, and it is fast and thorough. The
problem is what it does with a link it cannot reach. From its own issue tracker:

- Someone checking a site behind Cloudflare asks for a FlareSolverr integration
  because "checking sites that use cloudflare can result in 403"
  ([lychee#1439](https://github.com/lycheeverse/lychee/issues/1439)).
- A blog with 600 pages and a thousand links takes "15+ minutes to check them
  all", and the author asks for a way to stop early
  ([lychee#2076](https://github.com/lycheeverse/lychee/issues/2076)).
- Checking fragments on GitHub fails with `Cannot find fragment` on links that
  work in a browser ([lychee#1729](https://github.com/lycheeverse/lychee/issues/1729)).
- `--offline`, `--scheme file` and `--skip-missing` all sound like "check local
  files only", and none of them quite means that
  ([lychee#668](https://github.com/lycheeverse/lychee/issues/668),
  [lychee#1646](https://github.com/lycheeverse/lychee/issues/1646)).

Every one of those is the same bug wearing a different hat: the tool treats
"the server did not give me a 200" as "the link is broken". A 403 from a bot
filter, a 429 from a rate limiter, a 503 during a deploy, a timeout on hotel
wifi — none of them mean the page is gone, and all of them turn a CI run red.

So this tool answers a different question. Not *did the request succeed*, but
**is this link broken for a reader**. It has five verdicts instead of two, and
only one of them fails the build.

## The five verdicts

| verdict | what it means | fails the build |
|---|---|---|
| `ok` | 2xx after following redirects, or a local file that exists, or an anchor that is there | no |
| `dead` | 404 / 410 / 451, a redirect loop, a host that does not resolve, a missing file, a missing `#anchor` | **yes** |
| `blocked` | 401 / 403 / 405 / 429 / 503 / 999 — the server refused this client, which says nothing about the page | no |
| `unknown` | timeout, TLS failure, connection refused, 5xx — could be permanent, could be a bad minute | no |
| `skipped` | `mailto:`, `tel:`, `javascript:`, an unsupported scheme, or the time budget ran out | no |

`--strict` promotes `blocked` and `unknown` to failures when you want the
paranoid version. The default is the one you can put in CI without it crying
wolf.

Two rules keep `dead` honest:

**A 404 is checked with a second method.** Many servers answer HEAD with 403 or
404 and GET with 200. If HEAD fails, the tool re-requests with GET before
deciding anything.

**A 404 is only dead if the host still serves its own root.** `crates.io`
answers 404 to every HTML page for clients it does not like — root included.
Asking a host about its front door costs one request, and it is the difference
between "12 dead links" and "this site does not want to talk to you".

## What it checks

- Markdown: inline links, images, reference definitions, autolinks, and inline
  HTML `href`/`src`. Links inside `code spans` and fenced blocks are not links.
- HTML: `href` and `src` attributes.
- Anchors: `#heading` against GitHub's slug rules, `#L12` against the line count
  of a local file, and `id=`/`name=` for HTML targets.
- Local paths: relative, absolute, `~`, and percent-encoded.

## What it does not do

- No config file. Eight flags, all optional.
- No remote inputs. Point it at files and directories; it does not fetch a page
  and crawl it.
- No HTML rendering. A page whose anchors are drawn by JavaScript will report
  `unknown`, not `dead`.
- No `--accept 200..=204,429` grammar. The status table is a constant you can
  read in ten seconds.

## Usage

```bash
python linkcheck.py docs/                     # everything, local and remote
python linkcheck.py README.md --only local    # no network calls at all
python linkcheck.py . --only external         # only the links that leave the repo
python linkcheck.py docs/ --json              # machine-readable, one record per link
python linkcheck.py docs/ --deadline 60       # stop after a minute, report what is left
python linkcheck.py docs/ --exclude 'localhost|127\.0\.0\.1'
python linkcheck.py docs/ --strict            # blocked and unknown fail too
```

Exit codes: `0` nothing dead, `1` dead links (or `blocked`/`unknown` under
`--strict`), `2` usage error.

### Options

| flag | default | what it does |
|---|---|---|
| `--only all\|local\|external` | `all` | `local` never opens a socket |
| `--timeout SECONDS` | 10 | per request |
| `--jobs N` | 8 | parallel checks; at most 4 in flight per host |
| `--deadline SECONDS` | none | stop and report the rest as skipped |
| `--no-fragments` | off | skip `#anchor` verification |
| `--exclude REGEX` | none | repeatable |
| `--strict` | off | blocked/unknown fail the build |
| `--json` | off | machine-readable output |

## Anchors are where links rot quietly

A renamed heading breaks every link to it, and nothing in the file that did the
linking changes. So `#anchor` is checked by default, using GitHub's slug rules:
lowercase, punctuation dropped, spaces to hyphens, duplicates get `-1`, `-2`.

```
## Two Words        -> #two-words
## What's new?      -> #whats-new
## 第一部分 安装      -> #第一部分-安装
## Usage            -> #usage, then #usage-1 for the second one
```

A `#L119` anchor on a web page is reported `unknown`, not `dead`: code hosts
draw those line anchors with JavaScript, so their absence from the HTML proves
nothing. Guessing here is exactly the bug that
[lychee#1729](https://github.com/lycheeverse/lychee/issues/1729) is about.

## Speed

`PER_HOST = 4`, one shared pool of `--jobs` threads, and duplicate URLs are
checked once — a doc tree that mentions the same URL thirty times makes one
request, not thirty.

| | linkcheck | lychee 0.24.2 |
|---|---|---|
| 60 distinct links, one host | 0.84 s | 3.93 s |
| 1000 distinct links, one host | 0.72 s | — |
| same corpus, 104 links / 24 distinct | 4.8 s (4.8-5.0) | 6.1-20.1 s across three runs |
| cold start (`--version`) | 0.76 s | 0.75 s |

All numbers on Windows, Python 3.13, loopback server. The 4.8 s row is dominated
by one deliberately slow endpoint that sleeps 4 s; the run time is stable to
0.2 s, while lychee's varied by 14 s between identical runs.

## Tests

```bash
python -m unittest discover -s tests
```

102 tests, no network required — the suite starts its own HTTP server on a
random port and covers every status code, the HEAD/GET fallback, redirect
loops, anchors, the read-cap regression, and the "host 404s its own root" rule.

## Install

There is nothing to install.

```bash
curl -O https://raw.githubusercontent.com/z15346183030/linkcheck/main/linkcheck.py
python linkcheck.py --version
```

Python 3.10 or newer. Standard library only.

## Licence

MIT.
