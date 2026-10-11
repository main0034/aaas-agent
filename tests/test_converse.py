import argparse
import asyncio
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from harness import checks as ck
from harness import converse as cv
from harness import deliver as dl
from harness import main as hm


def make(tmp_path: Path) -> cv.Conversation:
    args = argparse.Namespace(owner="o", app_repo="aaas-app-demo", model=None, max_turns=60, checks_timeout=60,
                              runs_dir=str(tmp_path), run_id="r", port=0, app_url="", task="converse",
                              request=None, change=None, spec_testers=1, auto_merge=False, fix_rounds=0,
                              skip_local_checks=False, non_interactive=False, at=None)
    deployments = tmp_path / "deployments"
    (deployments / "agent").mkdir(parents=True)
    (deployments / "agent" / "intake.md").write_text("runbook")
    conv = cv.Conversation(args, tmp_path, deployments)
    conv.number = 1
    req = tmp_path / "request-1" / "item-archive.md"
    req.parent.mkdir()
    req.write_text("# Archive done items\n\n## Rules\n- x\n")
    conv.ready = {"name": "item-archive", "title": "Archive done items", "path": req}
    return conv


def run_go(conv, monkeypatch, result, delivery):
    seen = {}

    async def fake_change(ns, progress=None, result_out=None, **kw):  # noqa: ANN001
        seen["ns"] = ns
        progress("Building it")
        (kw.get("result") if "result" in kw else result_out).update(result)
        return 0 if result.get("green") else 1

    async def fake_report(facts):  # noqa: ANN001
        seen["facts"] = facts
        return "Your change is live."

    def fake_deliver(owner, pr, deployment, progress):  # noqa: ANN001
        seen["deliver"] = (owner, pr, deployment)
        progress("Going live")
        return delivery

    monkeypatch.setattr(hm, "run_change", lambda ns, progress=None, result=None: fake_change(ns, progress, result))
    monkeypatch.setattr(dl, "deliver", fake_deliver)
    monkeypatch.setattr(dl, "deployment_path", lambda app: "deployments/dev/demo")
    monkeypatch.setattr(conv, "report", fake_report)
    asyncio.run(conv.go())
    return seen


def test_go_runs_the_change_delivers_and_reports(tmp_path, monkeypatch):
    conv = make(tmp_path)
    pr = ck.PullRequest("o", "aaas-app-demo", 7)
    seen = run_go(conv, monkeypatch, {"pr": pr, "green": True, "merged": True, "app": tmp_path, "end_reason": "ok"},
                  dl.Delivery(True, "https://github.com/o/aaas-deployments/pull/31", app_url="https://app.io"))
    ns = seen["ns"]
    assert (ns.task, ns.change, ns.spec_testers, ns.fix_rounds, ns.auto_merge) == ("create-change", "item-archive", 2, 2, True)
    assert ns.request.startswith("# Archive done items") and ns.run_id == "change-1"
    assert seen["deliver"][1] == pr
    assert "Live: yes" in seen["facts"] and "https://app.io" in seen["facts"]
    s = conv.state()
    assert s["phase"] == "done" and not s["busy"] and s["messages"][-1]["text"] == "Your change is live."
    assert [x["text"] for x in s["steps"]] == ["Building it", "Going live", "Done"]
    assert (tmp_path / "facts-1.md").is_file() and (tmp_path / "converse.md").is_file()


def test_a_gated_change_is_not_delivered(tmp_path, monkeypatch):
    conv = make(tmp_path)
    pr = ck.PullRequest("o", "aaas-app-demo", 7)
    seen = run_go(conv, monkeypatch, {"pr": pr, "green": True, "blockers": ["adds or changes a migration (m.cs)"],
                                      "app": tmp_path, "end_reason": "not auto-merged"}, None)
    assert "deliver" not in seen
    assert "stores its data" in seen["facts"] and "Live:" not in seen["facts"]
    assert conv.state()["steps"][-1]["text"] == "Stopped"


def test_the_page_and_state_are_served(tmp_path):
    conv = make(tmp_path)
    conv.messages = [{"role": "assistant", "text": "Hello", "at": 0}]
    loop = asyncio.new_event_loop()
    server = ThreadingHTTPServer(("127.0.0.1", 0), cv.make_handler(conv, loop))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        page = urllib.request.urlopen(base + "/").read().decode()
        assert "Go ahead" in page
        state = json.loads(urllib.request.urlopen(base + "/api/state").read())
        assert state["ready"] is True and state["title"] == "Archive done items"
        assert state["messages"][0]["text"] == "Hello"
    finally:
        server.shutdown()
        loop.close()
