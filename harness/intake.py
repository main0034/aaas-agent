"""
Intake: a conversation with the app's owner that ends in one request file.

Roadmap step 5 (the conversation plane, OQ-4/OQ-22). The intake role follows
`aaas-deployments/agent/intake.md`. It can read an isolated checkout of the app
and write one file, and nothing else: no shell, no token, no network through git.

What decides something here is deterministic and testable without a model:

* `check_request` - is the file a complete request by `requests/TEMPLATE.md`'s
  rules? The "go ahead" button appears only when it is (D-17: the harness decides
  completeness, not the agent saying so).
* `report_facts` - the facts the reporter turns into plain words. The reporter is
  given these and nothing else, so it cannot describe what did not happen.
"""

from __future__ import annotations

import re
from pathlib import Path

from .change import slugify

RUNBOOK = "agent/intake.md"

REQUIRED_SECTIONS = ("What and why", "Rules", "Interface")
_SECTION_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_ROUTE_RE = re.compile(r"^\s*-\s+`(GET|POST|PUT|PATCH|DELETE) /[^`]*`", re.MULTILINE)
# `<what the change is>`, outside backticks (where `List<Item>` may be legitimate).
_PLACEHOLDER_RE = re.compile(r"<[A-Za-z][^<>\n]*>")


def sections(text: str) -> dict[str, str]:
    """`## Heading` -> body, keyed by the heading without any parenthesis."""
    out: dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        name = re.sub(r"\s*\(.*\)$", "", m.group(1)).strip()
        out[name] = text[m.end():end].strip()
    return out


def check_request(text: str) -> list[str]:
    """Why this is not yet a complete request. Empty means it is."""
    problems: list[str] = []
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    if not lines or not lines[0].startswith("# "):
        problems.append("the first line must be the title, as `# <what the change is>`")
    found = sections(text)
    for name in REQUIRED_SECTIONS:
        if name not in found:
            problems.append(f"the section `## {name}` is missing")
        elif not found[name]:
            problems.append(f"the section `## {name}` is empty")
    rules = found.get("Rules", "")
    if rules and not re.search(r"^\s*-\s+\S", rules, re.MULTILINE):
        problems.append("`## Rules` has no rule: each is a line starting with `- `")
    interface = found.get("Interface", "")
    if interface and not _ROUTE_RE.search(interface):
        problems.append("`## Interface` has no route: each starts with `- ` and the method and path in backticks")
    prose = re.sub(r"`[^`\n]*`", "", text)
    if "<!--" in text or _PLACEHOLDER_RE.search(prose) or "/...`" in text:
        problems.append("a template comment (`<!--`) or `<placeholder>` is still in the file")
    return problems


def pick_request(directory: Path) -> Path | None:
    """The request file the intake wrote: the most recently changed `.md` in its directory."""
    files = [f for f in directory.glob("*.md") if f.is_file()]
    return max(files, key=lambda f: f.stat().st_mtime) if files else None


def change_name(path: Path) -> str:
    return slugify(path.stem)


def briefing(app: Path, out_dir: Path) -> str:
    return f"""
# Workspace

You are running in a container with no credentials, no shell and no network.

| Path | You may |
|---|---|
| `{app}` | read only - the application as it runs today. Your working directory |
| `{out_dir}` | write the request here, as one file: `{out_dir}/<short-name>.md`, where the short name is lower case with dashes and says what the change is (`item-archive`). Nothing else |

Use the Read, Glob and Grep tools to read, and Write or Edit for the request file.

# Your task

Follow your instructions above: read the application first, then talk with the
user. Their first message follows.
""".strip()


def incomplete_note(path: Path, problems: list[str]) -> str:
    items = "\n".join(f"- {p}" for p in problems)
    return f"""
(From the harness, not the user.) `{path.name}` is not a complete request yet:

{items}

Fix the file. Then answer the user's last message as you would have; do not
mention this note.
""".strip()


# -- reporting back -------------------------------------------------------------

def report_facts(
    *,
    title: str,
    built: bool,
    checks: str,
    merged: bool,
    merge_blockers: str = "",
    live: bool = False,
    app_url: str = "",
    deploy_problem: str = "",
    stopped_because: str = "",
) -> str:
    """What happened, as facts. The reporter writes the user's message from this alone."""
    facts = [f"- The request: {title}"]
    facts.append(f"- Built: {'yes' if built else 'no'}")
    if built:
        facts.append(f"- Every check passed: {checks}")
    if merged:
        facts.append("- Accepted into the app without anyone needing to look: yes")
    elif merge_blockers:
        facts.append(f"- Accepted into the app: not yet - a person has to look it over first, because it {merge_blockers}")
    if merged:
        facts.append(f"- Live: {'yes' if live else 'no'}")
        if app_url:
            facts.append(f"- The app's address: {app_url}")
        if deploy_problem:
            facts.append(f"- Why it did not go live: {deploy_problem}")
    if stopped_because:
        facts.append(f"- Where it stopped, in the operator's words: {stopped_because}")
    facts.append("- If it did not go live, the version the user had before keeps running, unchanged, and the operator has the details")
    return "\n".join(facts)


def report_prompt(facts: str) -> str:
    return f"""
The work on the user's request has finished. Here is what happened, as facts:

{facts}

Write your message to the user now, following section 4 of your instructions
("Reporting back"). Only the message: it is shown to them as it is.
""".strip()


def blocker_words(blockers: list[str]) -> str:
    """change.merge_blockers' reasons, without file paths, for report_facts."""
    reasons = sorted({re.sub(r"\s*\(.*\)$", "", b) for b in blockers})
    mapping = {
        "adds or changes a migration": "changes how the app stores its data",
        "changes a dependency": "adds a new building block to the app",
    }
    words = [mapping.get(r, "touches a part of the app that is reviewed by hand") for r in reasons]
    return " and ".join(sorted(set(words)))
