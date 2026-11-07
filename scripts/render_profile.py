#!/usr/bin/env python3
"""render_profile.py - refresh the live assets of the MrAryanCode profile.

Reads the account and its repositories from the GitHub API and rewrites two
files: assets/stats.svg (the number tiles) and the fact pills inside
assets/banner.svg. README.md and every other asset are hand-written and are
never touched. The profile workflow runs this on a schedule and commits the
two files back.

Usage
  python scripts/render_profile.py [--root DIR] [--write] [--assets-only]
                                   [--json] [--fixture PATH]

  --write        write assets/stats.svg and the banner's fact pills under
                 the root. Default: dry run, writes nothing
  --assets-only  accepted for the workflow; the assets are the only output
  --json         print the fetched facts as JSON to stdout, for debugging
  --fixture      read the API responses from a recorded JSON file instead of
                 the network. Must contain the same shape the fetcher returns

Credentials: the token is read from the environment (GITHUB_TOKEN, as GitHub
Actions provides it) or from git's credential helper in a subprocess. It is
never placed in argv, never written to a file, never logged, and never
echoed. If no token is found the generator still runs against the public API
at a lower rate limit, which is enough for this account.

Exit codes: 0 wrote or would-write, 1 fetch failure, 2 usage.
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
    file, not in a log.
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


def _svg_escape(s):
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def write_banner_facts(facts, picked, windowed, out_path):
    """Rewrite the banner's fact pills from this run's data.

    The pills carry account-scope facts, and because this runs on every
    render the numbers on the artwork cannot go stale behind the text.
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
              "its fact pills would go stale", file=sys.stderr)
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


def main(argv=None):
    ap = argparse.ArgumentParser(description=((__doc__ or "").splitlines() or [""])[0])
    ap.add_argument("--root", default=".", help="repository root (default .)")
    ap.add_argument("--write", action="store_true", help="write the files")
    ap.add_argument("--assets-only", action="store_true",
                    help="write only assets/stats.svg and the banner fact pills "
                         "(this is the only output)")
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

    if ns.json:
        print(json.dumps(
            {"facts": facts, "picked": [p["full_name"] for p in picked]},
            indent=2, ensure_ascii=False))
        return 0

    if not ns.write:
        print(f"render_profile: dry run, {len(picked)} projects, "
              f"{len(windowed)} active; nothing written")
        return 0

    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    write_stats_svg(facts, assets / "stats.svg")
    write_banner_facts(facts, picked, windowed, assets / "banner.svg")
    print(f"render_profile: wrote live assets in {assets} "
          f"({len(picked)} projects)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
