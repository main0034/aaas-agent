from pathlib import Path

from harness import intake as it
from harness.policy import ToolPolicy

GOOD = """# Archive done items

## What and why

We want old done items out of the way without deleting them.

## Rules

- An item can be archived only when it is done. Otherwise it is refused, and nothing changes.
- Archived items are left out of `GET /items`.

## Interface (fixed - build exactly this)

- `POST /items/{id}/archive` returns `204`. Unknown item: `404`. Not done: `409`.
- `GET /items/archived` returns `200` with a JSON array of items, in the same shape `GET /items` returns them.

## Examples

- A done item is archived; it no longer appears in `GET /items`.
"""


def test_a_complete_request_passes():
    assert it.check_request(GOOD) == []


def test_template_comments_and_placeholders_are_refused():
    assert it.check_request(GOOD + "\n<!-- One or two sentences -->\n")
    assert it.check_request(GOOD.replace("# Archive done items", "# <What the change is, in a few words>"))
    # ...but a generic type inside backticks is not a placeholder
    assert it.check_request(GOOD + "\n- Returns `List<Item>`.\n") == []


def test_the_template_route_placeholder_is_refused():
    assert it.check_request(GOOD.replace("`GET /items/archived`", "`GET /...`"))


def test_missing_or_empty_sections():
    no_rules = GOOD.replace("## Rules", "## Notes")
    assert any("Rules" in p for p in it.check_request(no_rules))
    empty_interface = GOOD.split("## Interface")[0] + "## Interface (fixed - build exactly this)\n\n## Examples\n\n- x\n"
    assert any("Interface" in p for p in it.check_request(empty_interface))


def test_interface_needs_a_route_and_rules_need_a_bullet():
    prose_interface = GOOD.replace("- `POST /items/{id}/archive`", "The route `POST /items/{id}/archive`").replace(
        "- `GET /items/archived`", "And `GET /items/archived`")
    assert any("no route" in p for p in it.check_request(prose_interface))
    prose_rules = GOOD.replace("- An item can", "An item can").replace("- Archived items", "Archived items")
    assert any("no rule" in p for p in it.check_request(prose_rules))


def test_title_must_come_first():
    assert any("title" in p for p in it.check_request("Some text\n\n" + GOOD))


def test_sections_ignore_the_parenthesis():
    assert "Interface" in it.sections(GOOD)


def test_pick_request_takes_the_newest_markdown(tmp_path: Path):
    assert it.pick_request(tmp_path) is None
    (tmp_path / "old.md").write_text("a")
    newer = tmp_path / "item-archive.md"
    newer.write_text("b")
    import os
    os.utime(tmp_path / "old.md", (1, 1))
    assert it.pick_request(tmp_path) == newer
    assert it.change_name(newer) == "item-archive"


def test_report_facts_say_what_happened_and_nothing_else():
    live = it.report_facts(title="Archive", built=True, checks="yes", merged=True, live=True, app_url="https://x")
    assert "Live: yes" in live and "https://x" in live and "Why it did not" not in live
    gated = it.report_facts(title="Archive", built=True, checks="yes", merged=False,
                            merge_blockers=it.blocker_words(["adds or changes a migration (src/App/Migrations/x.cs)"]))
    assert "changes how the app stores its data" in gated and "Live:" not in gated
    failed = it.report_facts(title="A", built=True, checks="yes", merged=True, live=False, deploy_problem="cause: 23505")
    assert "Why it did not go live: cause: 23505" in failed


def test_blocker_words_hide_paths():
    words = it.blocker_words(["changes a dependency (Directory.Packages.props)", "touches README.md"])
    assert "Directory" not in words and "README" not in words
    assert "new building block" in words and "reviewed by hand" in words


def test_intake_policy_has_no_shell_and_one_directory(tmp_path: Path):
    out = tmp_path / "request-1"
    out.mkdir()
    p = ToolPolicy(writable_roots=[out], protected_globs=(), offline=True, shell=False)
    assert not p.check("Bash", {"command": "ls"}).allow
    assert p.check("Write", {"file_path": str(out / "item-archive.md")}).allow
    assert not p.check("Write", {"file_path": str(tmp_path / "app" / "src" / "x.cs")}).allow
    assert p.check("Read", {"file_path": "/etc/passwd"}).allow  # reads are the container's business
    assert len(p.denials) == 2


def test_check_from_the_command_line(tmp_path) -> None:  # noqa: ANN001 - workbench.sh check
    import subprocess
    import sys

    good = tmp_path / "ok.md"
    good.write_text("# T\n\n## What and why\nx\n\n## Rules\n- r\n\n## Interface\n- `GET /items` - 200\n")
    bad = tmp_path / "bad.md"
    bad.write_text("# T\n")
    run = lambda p: subprocess.run([sys.executable, "-m", "harness.intake", str(p)], capture_output=True, text=True)  # noqa: E731
    assert run(good).returncode == 0
    out = run(bad)
    assert out.returncode == 1 and "`## Rules` is missing" in out.stdout
