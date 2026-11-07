#!/usr/bin/env python3
"""render_profile.py - generate the MrAryanCode profile face from live data (T-011).

This replaces the hand-written profile README. The face is a view of the whole
account, rebuilt from the GitHub API on a schedule and committed back to the
profile repository. The owner never edits it by hand: a new project enters the
face on its own and a stale one leaves on its own.

Owner rules enforced here (A-0044), and re-enforced by
checks/check_profile_face.py so a future edit cannot quietly break them:

  1  no single language, stack, platform, protocol or licence is named
  2  no specific project is named directly - top projects are derived from
     the API each run, linked and labelled
  3  no fixed number standing for a quantity that varies in reality: every
     number printed is a value fetched during this run
  4  English only
  5  every line describes the whole account, not one project

Usage
  python scripts/render_profile.py [--root DIR] [--write] [--json]
                                   [--fixture PATH]

  --write     write the face - README.md plus assets/stats.svg and the
              banner's fact pills - into the face directory this checkout
              carries: content/profile/ in the kit workspace, the repository
              root in the profile repository (which is where GitHub renders
              it). Default: dry run, prints to stdout, writes nothing
  --json      print the fetched facts as JSON to stdout, for debugging
  --fixture   read the API responses from a recorded JSON file instead of the
              network. Used by the offline self-check and by CI when the
              network is unavailable. Must contain the same shape the fetcher
              returns.

Credentials (decision D-014): the token is read from the environment
(GITHUB_TOKEN, as GitHub Actions provides it) or from git's credential
helper in a subprocess. It is never placed in argv, never written to a file,
never logged, and never echoed. If no token is found the generator still runs
against the public API at a lower rate limit, which is enough for this
account.

Exit codes: 0 wrote or would-write, 1 fetch or render failure, 2 usage.
"""

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone

ACCOUNT = "MrAryanCode"
PROFILE_REPO = "MrAryanCode/MrAryanCode"
# How many projects the face lists. Beyond this a project drops off the face
# automatically as newer or more starred ones arrive.
TOP_N = 6
# A project counts as active if it saw a push inside this many days.
ACTIVE_WINDOW_DAYS = 90


def _token():
    """Read a GitHub token: environment first, then git's credential helper.

    GitHub Actions hands the workflow a short-lived GITHUB_TOKEN in the
    environment and configures no git credential helper, so the helper path
    alone finds nothing there and the render falls back to the unauthenticated
    API, which a shared runner IP gets rate-limited out of within minutes.
    The secret is returned as a value we never print: not in argv, not in a
    file, not in a log (decision D-014).
    """
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    try:
        # Popen, not subprocess.run: run(input=..., timeout=...) ignores its
        # timeout on Windows when the child waits on stdin, and a hung
        # credential helper must not hang the render.
        proc = subprocess.Popen(
            ["git", "credential", "fill"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        out, _ = proc.communicate(
            "protocol=https\nhost=github.com\n\n", timeout=20)
    except Exception:
        return None
    for line in (out or "").splitlines():
        if line.lower().startswith("password="):
            return line.partition("=")[2].strip() or None
    return None


def _gh(path, token, params="", attempts=4):
    """One GET against the GitHub REST API. Returns parsed JSON or None.

    A flaky link must not take the whole render down: transport failures are
    retried with a backoff, and a genuine HTTP answer is returned as-is.
    """
    import urllib.request
    import urllib.error

    last = None
    for i in range(attempts):
        try:
            return _gh_once(path, token, params)
        except RuntimeError as e:
            last = e
            if i == attempts - 1:
                raise
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"GET {path} failed after {attempts} attempts: {last}")


def _gh_once(path, token, params=""):
    """A single GET attempt. Raises RuntimeError on an HTTP status answer."""
    import urllib.request
    import urllib.error

    url = f"https://api.github.com{path}{params}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "mraryancode-profile-render")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    # An HTTP error is a real answer from the API (bad token, rate limit,
    # missing repo) and hiding it behind None turns a diagnosable failure into
    # "cannot read the account object". Only transport-level noise is retried.
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GET {path} -> HTTP {e.code} {e.reason}") from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as e:
        # Returning None here would burn every retry with nothing to go on,
        # so the last attempt raises with the real cause attached.
        raise RuntimeError(
            f"GET {path} -> transport failure: {type(e).__name__}: {e}"
        ) from e


def fetch(token):
    """Collect every fact the face needs. Returns one dict, or raises."""

    # _gh() now raises with the real cause (HTTP status or transport error), so
    # these calls fail loudly instead of collapsing into a generic message that
    # hides what actually went wrong. The only falsy answers left are genuine
    # empty responses.
    account = _gh(f"/users/{ACCOUNT}", token)
    if not account:
        raise RuntimeError(
            f"the API answered {ACCOUNT!r} with an empty account object"
        )

    repos = _gh(f"/users/{ACCOUNT}/repos?per_page=100&type=owner&sort=updated", token)
    if repos is None:
        raise RuntimeError(
            f"the API answered the repository list with an empty body"
        )

    facts = {
        "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account": {
            "login": account.get("login", ACCOUNT),
            "name": account.get("name") or ACCOUNT,
            "bio": (account.get("bio") or "").strip(),
            "public_repos": account.get("public_repos", 0),
            "followers": account.get("followers", 0),
            "created_at": account.get("created_at"),
        },
        "projects": [],
    }

    now = datetime.now(timezone.utc)
    for r in repos:
        pushed = r.get("pushed_at")
        active_days = None
        if pushed:
            try:
                dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
                active_days = max(0, (now - dt).days)
            except ValueError:
                active_days = None
        description = (r.get("description") or "").strip()
        facts["projects"].append(
            {
                "name": r.get("name", ""),
                "full_name": r.get("full_name", ""),
                "url": r.get("html_url", ""),
                "description": description,
                "stars": r.get("stargazers_count", 0),
                "forks": r.get("forks_count", 0),
                "language": r.get("language"),   # used for ranking only, never printed
                "archived": bool(r.get("archived")),
                "fork": bool(r.get("fork")),
                "pushed_at": pushed,
                "active_days": active_days,
                "homepage": (r.get("homepage") or "").strip(),
                "topics": r.get("topics") or [],
            }
        )
    return facts


def rank(projects):
    """Pick the face's projects. Ranking only; nothing here is printed.

    The heuristic is documented so the face is predictable: an owned, non-
    archived, non-fork repository that has seen a push inside ACTIVE_WINDOW_DAYS
    outranks everything else, and stars break ties. That means a fresh project
    enters the face on its next push, and a dormant one falls off without the
    owner touching anything.
    """
    owned = [p for p in projects if not p["fork"] and not p["archived"]]
    windowed = [
        p for p in owned
        if p["active_days"] is not None and p["active_days"] <= ACTIVE_WINDOW_DAYS
    ]

    def key(p):
        active = 0 if p in windowed else 1
        return (active, -p["stars"])

    picked = sorted(owned, key=key)[:TOP_N]
    return picked, windowed


ASCII_FOLD = {
    "\u2014": "-", "\u2013": "-", "\u2012": "-", "\u2010": "-",
    "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
    "\u00a0": " ", "\u00b7": "-", "\u2026": "...", "\u2192": "->",
    "\u2190": "<-", "\u2022": "-", "\u25cf": "-", "\u2713": "x",
    "\u00d7": "x", "\u2248": "~", "\u2264": "<=", "\u2265": ">=",
}


def ascii_only(text):
    """Fold the face to plain ASCII.

    Rule 4 says the face is English only. The generator copies strings that
    arrive from the API and from SVG artwork, and those carry typographic
    punctuation, so folding at the single point where text enters the template
    is the only way the rule can hold on every future run.
    """
    out = []
    for ch in text:
        if ch in ASCII_FOLD:
            out.append(ASCII_FOLD[ch])
        elif ord(ch) < 128:
            out.append(ch)
        else:
            # anything else non-ASCII is dropped rather than guessed at
            out.append("")
    return "".join(out)


def _svg_escape(s):
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _md_escape(s):
    return ascii_only((s or "")).replace("|", "\\|").replace("\n", " ").strip()


def _truncate(s, n):
    s = ascii_only((s or "")).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def render(facts, picked, windowed):
    """Return the full README text plus the provenance block contents."""

    acc = facts["account"]
    top_names = [p["name"] for p in picked]
    top_full = [p["full_name"] for p in picked]

    stars = sum(p["stars"] for p in picked)
    forks = sum(p["forks"] for p in picked)

    # Numbers the face prints. Every one is traceable to this run's fetch, and
    # the gate matches these against the prose (rule 3).
    numbers = {
        "repositories": str(acc["public_repos"]),
        "active_projects": str(len(windowed)),
        "listed_projects": str(len(picked)),
        "stars": str(stars),
        "followers": str(acc["followers"]),
    }
    # Each row prints its own star count and age, so each row's figures get
    # provenance too - rule 3 is per printed number, not per section.
    for p_ in picked:
        numbers[f"stars_{p_['name']}"] = str(p_["stars"])
        if p_["active_days"] is not None:
            numbers[f"days_{p_['name']}"] = str(p_["active_days"])

    # Provenance tokens: the words the gate allows on the face.
    #
    # These do NOT come from the repositories' topic tags. A topic tag is a
    # per-project label, and several of them are exactly the single-stack words
    # the owner banned (the account tags one project with a TLS flavour and a
    # certificate flavour). Promoting project topics to account-wide tokens
    # would defeat rule 1 while making the gate look like it approved them.
    #
    # So the token list is account-scope by construction: the account's own
    # chosen self-description words, minus anything on the rule-1 denylist.
    # Both lists live here, so an operator can see the trade in one place.
    ACCOUNT_SCOPE_TOKENS = [
        "open-source", "cli", "devops", "design",
    ]
    DENYLIST = {"tls", "ssl", "https", "http", "ssh", "dns", "certificates",
                "certificate", "acme", "letsencrypt", "nginx", "openssl"}
    tokens = sorted({t for t in ACCOUNT_SCOPE_TOKENS if t not in DENYLIST})

    lines = []
    a = lines.append

    a('<div align="center">')
    a('  <img src="assets/banner.svg" alt="MrAryanCode - a live view of this account" width="100%">')
    a('  <br><br>')
    a('  <img src="assets/typing.svg" alt="repositories, tools, guides, scripts" height="42">')
    a('  <br><br>')
    a('  <b>A living view of this account, rebuilt from the GitHub API on a schedule.</b><br>')
    a('  <sub>Nothing here is hand-maintained: the projects, the counts and the dates below')
    a('  are read fresh on every run, so the page is never older than its own data.</sub>')
    a('</div>')
    a('')
    a('<img src="assets/divider.svg" alt="" width="100%">')
    a('')
    a('<!--profile-face:account-scope -->')
    a('')
    a('## What this account is')
    a('')
    # Written from account-scope facts only. Per-project prose belongs in that
    # project's own README; rule 5 says the face never explains one project.
    a(f'MrAryanCode is a collection of small, self-contained open-source projects. '
      f'{acc["public_repos"]} public repositories, {len(windowed)} of them touched in the '
      f'active window. Each is maintained on its own, with its own issue tracker and its '
      f'own release stream, and there is no shared runtime to install across them.')
    a('')
    a('The list below is not curated by hand. It is ranked on every run by recent activity '
      f'first and stars second, so it shows what is actually alive right now. '
      f'{len(picked)} projects are shown; the rest are one run away from appearing.')
    a('')
    a('## In numbers')
    a('')
    a('<img src="assets/stats.svg" alt="Live account figures" width="100%">')
    a('')
    a('<img src="assets/divider.svg" alt="" width="100%">')
    a('')
    a('## Projects, ranked by activity')
    a('')
    a('| project | what it is | activity | stars |')
    a('|---|---|---|---|')
    for p in picked:
        name = _md_escape(p["name"])
        # Deliberately NOT the repository description. Descriptions are written
        # per project and routinely name a stack, which would put rule 1 and
        # rule 5 at the mercy of whatever text a future project description
        # happens to carry. The face shows how the account maintains each
        # project, not what a project claims about itself.
        if p["active_days"] is None:
            desc = "maintained in its own repository"
        else:
            desc = "maintained in its own repository"
        if p["active_days"] is None:
            act = "—"
        elif p["active_days"] == 0:
            act = "today"
        elif p["active_days"] == 1:
            act = "yesterday"
        else:
            act = f'{p["active_days"]}d ago'
        a(f'| [{name}]({p["url"]}) | {desc} | {act} | {p["stars"]} |')
    a('')
    a('<img src="assets/divider.svg" alt="" width="100%">')
    a('')
    a('## Follow along')
    a('')
    a(f'Announcements and write-ups land on the '
      f'[Telegram channel]({facts.get("channel_url", "https://t.me/MrAryanCode")}).')
    a('Every project keeps its own issue tracker; the fastest way to reach the account is')
    a('to open an issue on the project it concerns.')
    a('')
    a('<img src="assets/divider.svg" alt="" width="100%">')
    a('')
    a('<div align="center">')
    a('  <sub><b>MrAryanCode</b> · last updated '
      f'last run {facts["fetched_at"]}</sub>')
    a('</div>')
    a('')

    provenance = (
        "<!--profile-face:v1\n"
        f"tokens: {', '.join(tokens)}\n"
        "numbers: "
        + ", ".join(f"{k}={v}" for k, v in numbers.items())
        + "\n"
        f"projects: {', '.join(top_full)}\n"
        "-->"
    )

    body = ascii_only("\n".join(lines))
    # the provenance block goes at the very top, before the first visible line
    body = provenance + "\n" + body

    return body, numbers, tokens, top_full


def write_banner_facts(facts, picked, windowed, out_path):
    """Rewrite the banner's fact pills from this run's data.

    The pills used to name a licence, a language, two platforms and a protocol
    - a single stack, which the owner banned (rule 1). They now carry
    account-scope facts, and because this runs on every render the numbers on
    the artwork cannot go stale behind the text (rule 3).
    """
    acc = facts["account"]
    stars = sum(p["stars"] for p in picked)
    pills = [
        str(acc["public_repos"]) + " public repositories",
        str(len(windowed)) + " active this period",
        str(stars) + " stars on the list",
        "open source",
    ]

    W, GAP, X0, Y, H = 270, 12, 80, 292, 36
    parts = []
    x = X0
    for label in pills:
        w = int(len(label) * 8.6) + 34
        parts += [
            f'<rect x="{x}" y="{Y}" width="{w}" height="{H}" rx="18" '
            f'fill="#13203a" stroke="#27406b"/>',
            f'<text x="{x + w // 2}" y="{Y + 23}">{_svg_escape(label)}</text>',
        ]
        x += w + GAP

    block = ("<!--facts-start-->\n      " + "\n      ".join(parts)
             + "\n      <!--facts-end-->")
    svg = pathlib.Path(out_path)
    text = svg.read_text(encoding="utf-8")
    if "<!--BANNER_FACTS-->" in text:
        text = text.replace("<!--BANNER_FACTS-->", block, 1)
    elif 'id="banner-facts">' in text:
        # Replace whatever now sits inside the facts group - a leftover
        # marker block, or a start marker without its end (the first
        # insertion keyed the end marker on an exact indentation this file
        # does not have, so that banner froze and could never refresh) -
        # up to the group's closing tag. The scan is anchored on the group
        # itself, never on the whitespace around it.
        text = re.sub(
            r'(id="banner-facts">).*?(?=\s*</g>)',
            lambda m: m.group(1) + block,
            text, count=1, flags=re.S,
        )
    else:
        print("render_profile: banner group 'banner-facts' not found; "
              "its fact pills would go stale (rule 3)", file=sys.stderr)
    svg.write_text(text, encoding="utf-8")


def write_stats_svg(facts, out_path):
    """Regenerate the number tiles so the digits themselves are artwork."""

    acc = facts["account"]
    picked, windowed = rank(facts["projects"])
    stars = sum(p["stars"] for p in picked)

    tiles = [
        ("PUBLIC REPOSITORIES", str(acc["public_repos"])),
        ("ACTIVE THIS PERIOD", str(len(windowed))),
        ("STARS ON THE LIST", str(stars)),
        ("FOLLOWERS", str(acc["followers"])),
    ]

    W, H, TW, GAP, TOP = 1180, 136, 283, 16, 8
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
        f'viewBox="0 0 {W} {H}" role="img" aria-label="Live account figures">',
        '<title>Live account figures</title>',
        '<defs>',
        '<linearGradient id="t1" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#5eead4"/><stop offset="1" stop-color="#818cf8"/></linearGradient>',
        '<linearGradient id="t2" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#fbbf24"/><stop offset="1" stop-color="#f472b6"/></linearGradient>',
        '<linearGradient id="t3" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#a78bfa"/><stop offset="1" stop-color="#2dd4bf"/></linearGradient>',
        '<linearGradient id="t4" x1="0" y1="0" x2="1" y2="0">'
        '<stop offset="0" stop-color="#34d399"/><stop offset="1" stop-color="#22d3ee"/></linearGradient>',
        '</defs>',
        '<g font-family="ui-sans-serif, -apple-system, BlinkMacSystemFont, \'Segoe UI\', '
        'Roboto, \'Helvetica Neue\', Arial, sans-serif">',
    ]
    for i, (label, value) in enumerate(tiles):
        x = i * (TW + GAP)
        parts += [
            f'<rect x="{x}" y="{TOP}" width="{TW}" height="120" rx="14" fill="#101a30" stroke="#22314f"/>',
            f'<text x="{x+24}" y="{TOP+38}" font-size="13" font-weight="700" '
            f'letter-spacing="2.4" fill="#6e7b94">{_svg_escape(label)}</text>',
            f'<text x="{x+24}" y="{TOP+80}" font-size="34" font-weight="800" '
            f'fill="url(#t{i+1})">{_svg_escape(value)}</text>',
            f'<text x="{x+24}" y="{TOP+106}" font-size="13" fill="#6e7b94">live · this run</text>',
        ]
    parts += ['</g>', '</svg>', '']
    pathlib.Path(out_path).write_text("\n".join(parts), encoding="utf-8")


def face_dir(root):
    """Where the profile face lives in this checkout.

    The kit workspace keeps the face under content/profile/; the profile
    repository that GitHub renders carries it at the repository root. The
    workspace layout is recognised first so kit checkouts behave exactly as
    before, and the profile repository falls through to its root.
    """
    kit = pathlib.Path(root) / "content" / "profile"
    if kit.is_dir() and not (pathlib.Path(root) / "README.md").exists():
        return kit
    return pathlib.Path(root)


def main(argv=None):
    ap = argparse.ArgumentParser(description=((__doc__ or "").splitlines() or [""])[0])
    ap.add_argument("--root", default=".", help="repository root (default .)")
    ap.add_argument("--write", action="store_true", help="write the files")
    ap.add_argument("--assets-only", action="store_true",
                    help="write only the live assets (stats.svg, banner.svg, join.svg); "
                         "skip the README so the owner's hand-built face is preserved")
    ap.add_argument("--json", action="store_true", help="print fetched facts")
    ap.add_argument("--fixture", help="read API responses from this JSON file")
    ns = ap.parse_args(argv)

    root = pathlib.Path(ns.root)

    if ns.fixture:
        facts = json.loads(pathlib.Path(ns.fixture).read_text(encoding="utf-8"))
    else:
        token = _token()
        try:
            facts = fetch(token)
        except RuntimeError as e:
            print(f"render_profile: fetch failed: {e}", file=sys.stderr)
            return 1

    picked, windowed = rank(facts["projects"])
    body, numbers, tokens, top_full = render(facts, picked, windowed)

    if ns.json:
        print(json.dumps(
            {"facts": facts, "picked": [p["full_name"] for p in picked],
             "numbers": numbers, "tokens": tokens},
            indent=2, ensure_ascii=False))
        return 0

    if not ns.write:
        print(body)
        return 0

    if ns.assets_only:
        # Refresh only the live SVG assets. The owner's hand-built face
        # (README.md with the Telegram join banner) is preserved untouched;
        # the workflow commits assets only.
        face = face_dir(root)
        assets = face / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        write_stats_svg(facts, assets / "stats.svg")
        write_banner_facts(facts, picked, windowed, assets / "banner.svg")
        print(f"render_profile: wrote live assets in {assets} "
              f"({len(top_full)} projects, {len(numbers)} live numbers)")
        return 0

    face = face_dir(root) / "README.md"
    face.parent.mkdir(parents=True, exist_ok=True)
    face.write_text(body, encoding="utf-8")
    assets = face.parent / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    write_stats_svg(facts, assets / "stats.svg")
    write_banner_facts(facts, picked, windowed, assets / "banner.svg")
    print(f"render_profile: wrote {face} "
          f"({len(top_full)} projects, {len(numbers)} live numbers)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
