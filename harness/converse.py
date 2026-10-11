"""
The conversation plane, thinly: a local chat page in front of intake, create-change
and delivery. Roadmap step 5 (OQ-4, OQ-22).

    ./run.sh --task converse --app-repo aaas-app-demo
    open http://localhost:8765

One container, one conversation at a time, the same boundary as every other task:
the page is served from inside the container and run.sh publishes it on
127.0.0.1 only. The flow:

  1. **intake** (`agent/intake.md`): a multi-turn session that can read an isolated
     checkout of the app and write one file. `intake.check_request` decides when the
     file is a complete request; only then does the page offer "Go ahead"
  2. the user presses it: `create-change` with two spec-testers, two fix rounds and
     `--auto-merge` (D-26), exactly as from the command line, on the request text
  3. if the harness merged it: `deliver.deliver` follows the deployment PR to a green
     readiness gate (`applied.json` names the new image)
  4. a **reporter** session with no tools turns the facts (`intake.report_facts`) into
     the user's words

The page never shows git, a PR or a check name. Everything the operator needs is in
`runs/<id>/`: the intake transcript and request, the change's own run record, and
`converse.md` with the timings.

The HTTP server is the standard library's, in a thread; the agent sessions run on
the asyncio loop in the main thread. The page polls `/api/state`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from . import checks as ck
from . import deliver as dl
from . import intake as it
from . import workspace as ws
from .policy import ToolPolicy, make_pre_tool_use_hook
from .report import RunRecord

PAGE = Path(__file__).with_name("chat.html")
GREETING = (
    "Hello. What would you like to change in your app? Describe it the way you would to a "
    "colleague - what you want to be able to do, and why."
)
INTAKE_TOOLS = ["Read", "Glob", "Grep", "Write", "Edit"]
DENY_FOR_INTAKE = ["Bash", "WebFetch", "WebSearch", "Task", "NotebookEdit", "TodoWrite"]
MAX_FIXUPS = 2


class Conversation:
    def __init__(self, args: argparse.Namespace, run_dir: Path, deployments: Path) -> None:
        self.args = args
        self.run_dir = run_dir
        self.deployments = deployments
        self.lock = threading.Lock()
        self.messages: list[dict] = []
        self.steps: list[dict] = []
        self.phase = "intake"  # intake | working | done
        self.busy = False
        self.ready: dict | None = None  # {"name", "title", "path"} once the request is complete
        self.number = 0
        self.client: ClaudeSDKClient | None = None
        self.record: RunRecord | None = None
        self.policy: ToolPolicy | None = None
        self.scratch: list[Path] = []
        self.out_dir: Path | None = None
        self.log: list[str] = []

    # -- state for the page --------------------------------------------------

    def state(self) -> dict:
        with self.lock:
            return {
                "messages": list(self.messages),
                "steps": list(self.steps),
                "phase": self.phase,
                "busy": self.busy,
                "ready": bool(self.ready) and self.phase == "intake" and not self.busy,
                "title": self.ready["title"] if self.ready else "",
            }

    def _say(self, role: str, text: str) -> None:
        with self.lock:
            self.messages.append({"role": role, "text": text, "at": time.time()})

    def step(self, text: str) -> None:
        with self.lock:
            if self.steps and self.steps[-1]["text"] == text:
                return
            now = time.time()
            if self.steps:
                self.steps[-1]["done"] = now
            self.steps.append({"text": text, "at": now, "done": None})
        self._note(f"step: {text}")

    def _note(self, line: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
        self.log.append(f"- {stamp} {line}")
        print(f"\033[2m[converse {stamp}] {line}\033[0m", flush=True)
        (self.run_dir / "converse.md").write_text("# Conversation log\n\n" + "\n".join(self.log) + "\n", encoding="utf-8")

    # -- intake ----------------------------------------------------------------

    async def start_intake(self) -> None:
        self.number += 1
        n = self.number
        root = Path(tempfile.mkdtemp(prefix="intake-", dir=str(Path.home())))
        self.scratch.append(root)
        app = ws.clone_isolated(root / self.args.app_repo, self.args.owner, self.args.app_repo)
        self.out_dir = self.run_dir / f"request-{n}"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.policy = ToolPolicy(writable_roots=[self.out_dir], protected_globs=(), offline=True, shell=False)
        runbook = (self.deployments / it.RUNBOOK).read_text(encoding="utf-8")
        self.record = RunRecord(
            run_id=f"intake-{n}", directory=self.run_dir / f"intake-{n}", task="intake", request="(conversation)",
            metadata={"runbook": it.RUNBOOK, f"{self.args.app_repo} HEAD": ws.head_sha(app),
                      "model": self.args.model or "(CLI default)"},
        )
        options = ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code",
                           "append": runbook + "\n\n" + it.briefing(app, self.out_dir)},
            # `tools` is the whole tool set the CLI loads; the rest do not exist in the session.
            tools=INTAKE_TOOLS,
            allowed_tools=INTAKE_TOOLS,
            disallowed_tools=DENY_FOR_INTAKE,
            permission_mode="bypassPermissions",
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[make_pre_tool_use_hook(self.policy)])]},
            cwd=str(app),
            add_dirs=[str(self.out_dir)],
            max_turns=self.args.max_turns,
            model=self.args.model,
            setting_sources=None,
            env={"GH_TOKEN": ""},
        )
        self.client = ClaudeSDKClient(options=options)
        await self.client.connect()
        with self.lock:
            self.phase, self.ready, self.steps = "intake", None, []
            self.messages = [{"role": "assistant", "text": GREETING, "at": time.time()}]
        self._note(f"intake {n} started at {ws.head_sha(app)[:7]}")

    async def _turn(self, message: str) -> str:
        assert self.client and self.record
        self.record.start_turn()
        # The SDK's stream holds only the agent's side; without this the transcript of
        # the first conversation could not show what the user had actually said.
        self.record.append_message({"role": "user", "text": message})
        await self.client.query(message)
        texts: list[str] = []
        async for msg in self.client.receive_response():
            self.record.append_message(msg)
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, ToolUseBlock):
                        self.record.note_tool(block.name)
                    elif isinstance(block, TextBlock) and block.text.strip():
                        texts.append(block.text.strip())
            elif isinstance(msg, ResultMessage):
                self.record.sdk_turns += msg.num_turns
                self.record.cost_usd += msg.total_cost_usd or 0.0
                if msg.is_error:
                    self.record.errors.append(msg.result or "error")
        self.record.end_turn()
        # The last text block is the answer; earlier ones are the agent thinking aloud
        # between tool calls ("Let me look at..."), which the user should not see.
        return texts[-1] if texts else ""

    async def say(self, text: str) -> None:
        with self.lock:
            if self.busy or self.phase != "intake":
                return
            self.busy = True
            self.messages.append({"role": "user", "text": text, "at": time.time()})
        try:
            reply = await self._turn(text)
            for _ in range(MAX_FIXUPS):
                path = it.pick_request(self.out_dir)
                problems = it.check_request(path.read_text(encoding="utf-8")) if path else []
                if not path or not problems:
                    break
                self._note(f"request {path.name} incomplete: {'; '.join(problems)}")
                reply = await self._turn(it.incomplete_note(path, problems)) or reply
            path = it.pick_request(self.out_dir)
            ready = None
            if path and not it.check_request(path.read_text(encoding="utf-8")):
                body = path.read_text(encoding="utf-8")
                ready = {"name": it.change_name(path), "title": body.splitlines()[0][2:].strip(), "path": path}
                self._note(f"request ready: {path.name} (${self.record.cost_usd:.2f} so far)")
                self.record.end_reason = f"request written: {path.name}"
            with self.lock:
                self.ready = ready
            self._say("assistant", reply or "(no answer - please say that again)")
        except Exception as exc:  # noqa: BLE001 - the page must get an answer
            self._note(f"intake error: {exc!r}")
            self._say("assistant", "Something went wrong on my side. Please say that again.")
        finally:
            with self.lock:
                self.busy = False
            self.record.snapshot(self.policy.denials if self.policy else [])

    # -- the work --------------------------------------------------------------

    async def go(self) -> None:
        with self.lock:
            if self.busy or self.phase != "intake" or not self.ready:
                return
            self.busy, self.phase = True, "working"
            ready = dict(self.ready)
            self.messages.append({"role": "user", "text": "Go ahead.", "at": time.time()})
        request = ready["path"].read_text(encoding="utf-8")
        self._note(f"go: change {ready['name']}")
        started = time.time()
        await self._close_intake()

        from .main import run_change  # main imports nothing from here; avoid the cycle at import time

        ns = argparse.Namespace(**vars(self.args))
        ns.task, ns.change, ns.request = "create-change", ready["name"], request
        ns.spec_testers, ns.fix_rounds, ns.auto_merge = 2, 2, True
        ns.runs_dir, ns.run_id = str(self.run_dir), f"change-{self.number}"
        ns.non_interactive, ns.skip_local_checks, ns.at = True, False, None
        result: dict = {}
        try:
            code = await run_change(ns, progress=self.step, result=result)
        except Exception as exc:  # noqa: BLE001
            code, result["end_reason"] = 1, f"harness error: {exc!r}"
        self._note(f"create-change exit {code}: {result.get('end_reason', '')}")

        delivery = None
        if result.get("merged"):
            try:
                deployment = dl.deployment_path(result["app"])
                delivery = await asyncio.to_thread(dl.deliver, self.args.owner, result["pr"], deployment,
                                                   progress=self.step)
            except (dl.DeliveryError, ck.ChecksError, OSError) as exc:
                delivery = dl.Delivery(False, problem=f"harness error while delivering: {exc}")
            self._note(f"delivery: live={delivery.live} {delivery.pr_url} {delivery.problem}")
        self.step("Done" if delivery and delivery.live else "Stopped")
        facts = it.report_facts(
            title=ready["title"],
            built="pr" in result,
            checks="yes" if result.get("green") else "no",
            merged=bool(result.get("merged")),
            merge_blockers=it.blocker_words(result.get("blockers") or []),
            live=bool(delivery and delivery.live),
            app_url=(delivery.app_url if delivery else "") or self.args.app_url,
            deploy_problem=(delivery.problem if delivery and not delivery.live else ""),
            stopped_because="" if delivery and delivery.live else result.get("end_reason", ""),
        )
        (self.run_dir / f"facts-{self.number}.md").write_text(facts + "\n", encoding="utf-8")
        answer = await self.report(facts)
        total = time.time() - started
        self._note(f"finished in {int(total // 60)}m {int(total % 60)}s; change ${result.get('cost_usd', 0):.2f}")
        self._say("assistant", answer)
        with self.lock:
            self.phase, self.busy = "done", False

    async def report(self, facts: str) -> str:
        runbook = (self.deployments / it.RUNBOOK).read_text(encoding="utf-8")
        options = ClaudeAgentOptions(
            system_prompt={"type": "preset", "preset": "claude_code", "append": runbook},
            tools=[],  # none at all: it writes one message from the facts it is given
            disallowed_tools=[*INTAKE_TOOLS, *DENY_FOR_INTAKE],
            permission_mode="bypassPermissions",
            cwd=str(self.run_dir),
            max_turns=2,
            model=self.args.model,
            setting_sources=None,
            env={"GH_TOKEN": ""},
        )
        record = RunRecord(run_id=f"report-{self.number}", directory=self.run_dir / f"report-{self.number}",
                           task="report", request=facts)
        texts: list[str] = []
        try:
            async with ClaudeSDKClient(options=options) as client:
                record.start_turn()
                await client.query(it.report_prompt(facts))
                async for msg in client.receive_response():
                    record.append_message(msg)
                    if isinstance(msg, AssistantMessage):
                        texts += [b.text.strip() for b in msg.content if isinstance(b, TextBlock) and b.text.strip()]
                    elif isinstance(msg, ResultMessage):
                        record.cost_usd += msg.total_cost_usd or 0.0
                record.end_turn()
        except Exception as exc:  # noqa: BLE001
            self._note(f"reporter error: {exc!r}")
        finally:
            record.finish([])
        return texts[-1] if texts else "The work has finished, but I could not write up the result. The operator has the details."

    async def _close_intake(self) -> None:
        if self.record is not None and self.record.ended_at is None:
            self.record.finish(self.policy.denials if self.policy else [])
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self.client = None
        for d in self.scratch:
            shutil.rmtree(d, ignore_errors=True)
        self.scratch.clear()

    async def again(self) -> None:
        with self.lock:
            if self.busy or self.phase != "done":
                return
            self.busy = True
        try:
            await self.start_intake()
        finally:
            with self.lock:
                self.busy = False


def make_handler(conv: Conversation, loop: asyncio.AbstractEventLoop):  # noqa: ANN201
    page = PAGE.read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a) -> None:  # noqa: ANN002 - quiet; the state poll is every 2s
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/", "/index.html"):
                self._send(200, page, "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._send(200, json.dumps(conv.state()).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                body = {}
            if self.path == "/api/say":
                text = str(body.get("text", "")).strip()[:4000]
                if text:
                    asyncio.run_coroutine_threadsafe(conv.say(text), loop)
            elif self.path == "/api/go":
                asyncio.run_coroutine_threadsafe(conv.go(), loop)
            elif self.path == "/api/again":
                asyncio.run_coroutine_threadsafe(conv.again(), loop)
            else:
                self._send(404, b"not found", "text/plain")
                return
            self._send(202, b"{}", "application/json")

    return Handler


async def serve(args: argparse.Namespace) -> int:
    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = Path(args.runs_dir) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    print(f"\033[1mRun {run_id}\033[0m  ({run_dir})\nPreparing workspace…", flush=True)
    try:
        workspace = ws.prepare(run_dir / "workspace", owner=args.owner)
    except ws.WorkspaceError as exc:
        print(f"\033[31m{exc}\033[0m", flush=True)
        return 2
    if not (workspace.deployments / it.RUNBOOK).is_file():
        print(f"\033[31m{it.RUNBOOK} does not exist in the deployments repo.\033[0m", flush=True)
        return 2
    args.app_url = getattr(args, "app_url", "") or ""

    conv = Conversation(args, run_dir, workspace.deployments)
    await conv.start_intake()
    loop = asyncio.get_running_loop()
    server = ThreadingHTTPServer(("0.0.0.0", args.port), make_handler(conv, loop))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"\n\033[1mChat page: http://localhost:{args.port}\033[0m  (Ctrl-C to stop)\n", flush=True)
    try:
        while True:
            await asyncio.sleep(3600)
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        server.shutdown()
        await conv._close_intake()
    return 0
