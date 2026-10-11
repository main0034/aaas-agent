"""
Screens: where in the app a change shows up (step 5b, OQ-26).

The app's pages are Blazor components under `src/App/Components/Pages/`, each with an
`@page "<path>"` and a `<PageTitle>` (the app's AGENT.md, "Screens"). After a change
is merged, the harness tells the user where to look. It does not ask the builder:

  1. `changed` lists the pages the change added or changed, from the diff, with
     each one's title and the pages that link to it (read from the source)
  2. `check_live` opens each of them on the live app and requires `200` and HTML.
     A path with parameters is opened with a placeholder (`{id:int}` -> `0`): the
     page must exist even when the item does not
  3. `facts` turns that into lines for the reporter, which may describe only these

On 11 October the reporter told the user to "open an item" in an app with no screens
(finding 31). Everything here is deterministic (D-17); the network is behind `fetch`.
"""

from __future__ import annotations

import re
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

PAGES_DIR = "src/App/Components/Pages/"
PAGE_RE = re.compile(r'^\s*@page\s+"([^"]*)"', re.MULTILINE)
TITLE_RE = re.compile(r"<PageTitle>\s*(.*?)\s*</PageTitle>", re.DOTALL)
PARAM_RE = re.compile(r"\{([^}:?]+)(?::([^}?]+))?\??\}")


@dataclass
class Screen:
    path: str
    title: str
    file: str
    linked_from: list[str] = field(default_factory=list)


def probe_path(template: str) -> str:
    """A concrete path for a page template: `{id:int}` -> 0, anything else -> x."""
    return PARAM_RE.sub(lambda m: "0" if (m.group(2) or "").startswith(("int", "long")) else "x", template)


def link_prefix(template: str) -> str:
    """What a link to this page starts with: the path up to its first parameter."""
    return template.split("{", 1)[0]


def read_pages(app: Path) -> dict[str, tuple[list[str], str, str]]:
    """Every page in the checkout: file -> (paths, title, source)."""
    pages = {}
    for f in sorted((app / PAGES_DIR).glob("**/*.razor")):
        src = f.read_text(encoding="utf-8")
        paths = PAGE_RE.findall(src)
        if paths:
            title = TITLE_RE.search(src)
            pages[str(f.relative_to(app))] = (paths, title.group(1).strip() if title else f.stem, src)
    return pages


def describe(pages: dict[str, tuple[list[str], str, str]], files: list[str]) -> list[Screen]:
    """The pages among `files`, each with the titles of the pages that link to it."""
    screens = []
    for file in sorted(set(files)):
        if file not in pages:
            continue
        paths, title, _ = pages[file]
        for path in paths:
            prefix = link_prefix(path)
            needle = f'href="{prefix}' if "{" in path else f'href="{path}"'
            linked = sorted(
                t for other, (_, t, src) in pages.items() if other != file and needle in src
            )
            screens.append(Screen(path, title, file, linked))
    return screens


def changed(app: Path, base: str, head_sha: str) -> list[Screen]:
    """The pages a change added or changed, read from the app checkout at `head_sha`."""
    files = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AM", f"origin/{base}...{head_sha}", "--", PAGES_DIR],
        cwd=app, check=True, capture_output=True, text=True,
    ).stdout.split()
    return describe(read_pages(app), files)


def _fetch(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=90) as r:  # noqa: S310 - our own app's https URL
            return r.status, r.headers.get_content_type()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get_content_type() if e.headers else ""


def check_live(app_url: str, screens: list[Screen],
               fetch: Callable[[str], tuple[int, str]] = _fetch) -> list[str]:
    """Problems opening each screen on the live app. Empty means every one opened."""
    problems = []
    for s in screens:
        url = app_url.rstrip("/") + probe_path(s.path)
        try:
            status, ctype = fetch(url)
        except OSError as exc:
            problems.append(f"{s.path}: {exc}")
            continue
        if status != 200 or ctype != "text/html":
            problems.append(f"{s.path}: {status} {ctype}".strip())
    return problems


def facts(screens: list[Screen], live_problems: list[str] | None) -> list[str]:
    """Lines for intake.report_facts. `live_problems` is None when nothing went live."""
    if not screens:
        return ["- Screens: none changed. Nothing new to look at in the app's pages; "
                "do not describe any screen, button or page"]
    lines = []
    for s in screens:
        where = "the start page" if s.path == "/" else f'the page "{s.title}"'
        if s.path != "/" and s.linked_from:
            where += ", opened from " + " or ".join(f'"{t}"' for t in s.linked_from)
            if "{" in s.path:
                where += " by choosing one"
        lines.append(f"- Where the user sees it: {where}")
    if live_problems:
        lines.append("- But the page did not open on the live app: " + "; ".join(live_problems))
    elif live_problems is not None:
        lines.append("- The page opens on the live app: checked")
    return lines
