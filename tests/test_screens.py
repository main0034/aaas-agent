from pathlib import Path

from harness import intake as it
from harness import screens as sc

HOME = """@page "/"
<PageTitle>Items</PageTitle>
<a href="/app/items/@item.Id">@item.Title</a>
"""
DETAIL = """@page "/app/items/{Id:int}"
<PageTitle>Edit item</PageTitle>
<a href="/">Back</a>
"""
REPORT = """@page "/app/report"
<PageTitle>This week</PageTitle>
"""


def pages(tmp_path: Path) -> dict:
    d = tmp_path / sc.PAGES_DIR
    d.mkdir(parents=True)
    (d / "Home.razor").write_text(HOME)
    (d / "ItemDetail.razor").write_text(DETAIL)
    (d / "Report.razor").write_text(REPORT)
    (d / "Errors.razor").write_text("<ul></ul>")  # a component, not a page
    return sc.read_pages(tmp_path)


def test_probe_path_fills_parameters():
    assert sc.probe_path("/app/items/{Id:int}") == "/app/items/0"
    assert sc.probe_path("/app/projects/{Name}/items/{Id:int?}") == "/app/projects/x/items/0"
    assert sc.probe_path("/") == "/"


def test_describe_finds_title_and_links(tmp_path):
    p = pages(tmp_path)
    screens = sc.describe(p, [f"{sc.PAGES_DIR}ItemDetail.razor", f"{sc.PAGES_DIR}Errors.razor", "src/App/Program.cs"])
    assert [(s.path, s.title, s.linked_from) for s in screens] == [("/app/items/{Id:int}", "Edit item", ["Items"])]


def test_unlinked_page_has_no_way_in(tmp_path):
    screens = sc.describe(pages(tmp_path), [f"{sc.PAGES_DIR}Report.razor"])
    assert screens[0].linked_from == []


def test_check_live_requires_200_html():
    s = [sc.Screen("/app/items/{Id:int}", "Edit item", "f"), sc.Screen("/", "Items", "g")]
    seen = []

    def fetch(url):
        seen.append(url)
        return (404, "application/problem+json") if "items" in url else (200, "text/html")

    assert sc.check_live("https://app.example/", s, fetch) == ["/app/items/{Id:int}: 404 application/problem+json"]
    assert seen == ["https://app.example/app/items/0", "https://app.example/"]


def test_facts_name_the_page_and_the_way_in():
    s = [sc.Screen("/app/items/{Id:int}", "Edit item", "f", ["Items"])]
    lines = sc.facts(s, [])
    assert lines[0] == '- Where the user sees it: the page "Edit item", opened from "Items" by choosing one'
    assert lines[1] == "- The page opens on the live app: checked"


def test_facts_without_screens_forbid_describing_any():
    assert "do not describe any screen" in sc.facts([], None)[0]


def test_report_facts_include_screens_only_when_built():
    lines = ["- Where the user sees it: the start page"]
    built = it.report_facts(title="t", built=True, checks="yes", merged=True, live=True, screens=lines)
    assert lines[0] in built
    not_built = it.report_facts(title="t", built=False, checks="no", merged=False, screens=lines)
    assert lines[0] not in not_built
