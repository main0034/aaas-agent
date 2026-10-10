#!/usr/bin/env python3
"""Every test is laid out Arrange / Act / Assert, with the comments to show it.

AGENT.md, "Tests: Arrange, Act, Assert". Checked deterministically (D-17), on the
test files a pull request adds or changes - merged files, including the change
record, are not re-checked.

A test is a method carrying [Fact] or [Theory]. Its body must:
  - be a block body, not `=> ...`, so there is somewhere to put the comments
  - contain `// Act` then `// Assert`, once each and in that order, or a single
    `// Act & Assert` when the call under test is the assertion
  - start with `// Arrange` if any statement comes before `// Act`

Usage:
  scripts/check-test-layout.py <base-ref>     # files changed against the base
  scripts/check-test-layout.py --files a.cs b.cs
"""

from __future__ import annotations

import re
import subprocess
import sys

TEST_ATTR = re.compile(r"^\s*\[(Fact|Theory)\b")
# `// Assert` alone, or with a note after a colon: `// Assert: 503, not a stack trace`.
MARKER = re.compile(r"^\s*//\s*(Arrange|Act & Assert|Act|Assert)\b(?:\s*:.*)?\s*$")


def strip_code(src: str) -> str:
    """Blank out comments' text, strings and chars, keeping line structure and `//` markers.

    Braces inside strings ("{x}", $"{y}") must not count when matching a body.
    Comment lines are kept verbatim so markers can be read; their braces are blanked.
    """
    out: list[str] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if src.startswith("//", i):
            j = src.find("\n", i)
            j = n if j == -1 else j
            out.append(re.sub(r"[{}]", " ", src[i:j]))
            i = j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j == -1 else j + 2
            out.append(re.sub(r"[^\n]", " ", src[i:j]))
            i = j
        elif src.startswith('"""', i) or (c in "$@" and src.startswith('"""', i + 1)):
            start = src.index('"""', i)
            j = src.find('"""', start + 3)
            j = n if j == -1 else j + 3
            out.append(re.sub(r"[^\n]", " ", src[i:j]))
            i = j
        elif c == '"' or (c in "$@" and i + 1 < n and src[i + 1] in '"$@'):
            j = i
            verbatim = False
            while j < n and src[j] in "$@":
                verbatim |= src[j] == "@"
                j += 1
            j += 1  # opening quote
            while j < n:
                if src[j] == "\\" and not verbatim:
                    j += 2
                    continue
                if src[j] == '"':
                    if verbatim and j + 1 < n and src[j + 1] == '"':
                        j += 2
                        continue
                    j += 1
                    break
                j += 1
            out.append(re.sub(r"[^\n]", " ", src[i:j]))
            i = j
        elif c == "'":
            j = i + 1
            while j < n and src[j] != "'":
                j += 2 if src[j] == "\\" else 1
            j += 1
            out.append(" " * (j - i))
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out)


def check_source(path: str, src: str) -> list[str]:
    errors: list[str] = []
    code = strip_code(src)
    lines = code.split("\n")
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line) + 1)

    for ln, line in enumerate(lines):
        if not TEST_ATTR.match(line):
            continue
        # The signature: from the line after the attribute(s) to the first `{` or `=>`.
        k = ln + 1
        while k < len(lines) and (lines[k].strip().startswith("[") or not lines[k].strip()):
            k += 1
        pos = offsets[k]
        brace, arrow = code.find("{", pos), code.find("=>", pos)
        name_m = re.search(r"(\w+)\s*\(", code[pos : (brace if brace != -1 else len(code))])
        name = name_m.group(1) if name_m else f"line {k + 1}"
        where = f"{path}:{k + 1}"
        if arrow != -1 and (brace == -1 or arrow < brace):
            errors.append(f"{where}: {name} has an expression body; use a block body with // Arrange, // Act, // Assert")
            continue
        depth, j = 0, brace
        while j < len(code):
            if code[j] == "{":
                depth += 1
            elif code[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        body = code[brace + 1 : j]
        markers: list[str] = []
        statements_before_act = False
        for raw in body.split("\n"):
            m = MARKER.match(raw)
            if m:
                markers.append(m.group(1))
            elif raw.strip() and not raw.strip().startswith("//") and not any(
                x.startswith("Act") for x in markers
            ):
                statements_before_act = True
        problems = []
        acts = [m for m in markers if m in ("Act", "Act & Assert")]
        if len(acts) != 1:
            problems.append("needs exactly one `// Act` (or `// Act & Assert`)")
        if "Act" in markers and markers.count("Assert") != 1:
            problems.append("needs exactly one `// Assert` after `// Act`")
        if "Act" in markers and "Assert" in markers and markers.index("Assert") < markers.index("Act"):
            problems.append("`// Assert` comes before `// Act`")
        if "Act & Assert" in markers and "Assert" in markers:
            problems.append("has both `// Act & Assert` and `// Assert`")
        if statements_before_act and (not markers or markers[0] != "Arrange"):
            problems.append("has statements before `// Act` but does not start with `// Arrange`")
        if markers.count("Arrange") > 1:
            problems.append("has more than one `// Arrange`")
        if problems:
            errors.append(f"{where}: {name} " + "; ".join(problems))
    return errors


def changed_files(base: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AM", f"{base}...HEAD", "--", "tests/*.cs", "changes/*.cs"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [f for f in out.split() if f.endswith(".cs")]


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--files":
        files = argv[2:]
    elif len(argv) == 2:
        files = changed_files(argv[1])
    else:
        print(__doc__, file=sys.stderr)
        return 2
    errors: list[str] = []
    for f in files:
        with open(f, encoding="utf-8") as fh:
            errors += check_source(f, fh.read())
    for e in errors:
        path, line, msg = e.split(":", 2)
        print(f"::error file={path},line={line}::{msg.strip()} (AGENT.md, 'Tests: Arrange, Act, Assert')")
    if not errors:
        print(f"Test layout OK: {len(files)} file(s) checked.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
