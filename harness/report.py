"""
Run record.

STATUS.md asks for one number - how long prompt-to-PR takes - and that number
is worthless without the context around it. So the record keeps: wall clock,
turns, cost, which tools were used how often, every policy refusal, and every
PR URL the run produced.

The tool histogram and the refusal list are the useful part. If a run takes
twenty minutes, the question is immediately "doing what?", and a count of
`git status` calls answers it faster than reading a transcript. The refusal
list is the feedback loop on the runbooks: a rule the agent keeps hitting is
usually a rule the runbook failed to explain.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PR_URL_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")


def _default(obj: Any) -> Any:
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


@dataclass
class Turn:
    index: int
    started_at: float
    ended_at: float | None = None
    tool_calls: list[str] = field(default_factory=list)

    @property
    def seconds(self) -> float:
        return (self.ended_at or time.time()) - self.started_at


@dataclass
class RunRecord:
    run_id: str
    directory: Path
    task: str
    request: str
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None
    turns: list[Turn] = field(default_factory=list)
    tool_counts: dict[str, int] = field(default_factory=dict)
    pr_urls: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    sdk_turns: int = 0
    errors: list[str] = field(default_factory=list)
    denials: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Fix-forward (Phase 5): one entry per agent session - the initial one and
    # each fix round - with what CI said about the commit it pushed.
    rounds: list[dict[str, Any]] = field(default_factory=list)
    # Why the run stopped, in one line. A capped run and a finished run both
    # used to end in "exit 1, no PR URL" with the cause only in the transcript
    # (FINDINGS.md #22).
    end_reason: str = ""

    # -- writing -----------------------------------------------------------

    def __post_init__(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._transcript = (self.directory / "transcript.jsonl").open("a", encoding="utf-8")

    def append_message(self, message: Any) -> None:
        try:
            payload = json.dumps(
                {"t": round(time.time() - self.started_at, 3), "message": message},
                default=_default,
            )
        except (TypeError, ValueError):
            payload = json.dumps({"t": round(time.time() - self.started_at, 3), "raw": str(message)})
        self._transcript.write(payload + "\n")
        self._transcript.flush()

    def note_tool(self, name: str) -> None:
        self.tool_counts[name] = self.tool_counts.get(name, 0) + 1
        if self.turns:
            self.turns[-1].tool_calls.append(name)

    def note_text(self, text: str) -> None:
        for match in PR_URL_RE.findall(text):
            if match not in self.pr_urls:
                self.pr_urls.append(match)

    def start_turn(self) -> Turn:
        turn = Turn(index=len(self.turns) + 1, started_at=time.time())
        self.turns.append(turn)
        return turn

    def end_turn(self) -> None:
        if self.turns:
            self.turns[-1].ended_at = time.time()

    def finish(self, denials: list[Any]) -> None:
        self.ended_at = time.time()
        self.denials = denials
        self._transcript.close()
        (self.directory / "report.md").write_text(self.render(), encoding="utf-8")
        (self.directory / "report.json").write_text(
            json.dumps(self.as_dict(), indent=2, default=_default), encoding="utf-8"
        )

    # -- rendering ---------------------------------------------------------

    @property
    def seconds(self) -> float:
        return (self.ended_at or time.time()) - self.started_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "request": self.request,
            "wall_clock_seconds": round(self.seconds, 1),
            "turns": len(self.turns),
            "sdk_turns": self.sdk_turns,
            "cost_usd": round(self.cost_usd, 4),
            "tool_counts": self.tool_counts,
            "pr_urls": self.pr_urls,
            "denials": [vars(d) if hasattr(d, "__dict__") else d for d in self.denials],
            "errors": self.errors,
            "metadata": self.metadata,
            "turn_seconds": [round(t.seconds, 1) for t in self.turns],
            "rounds": self.rounds,
            "end_reason": self.end_reason,
        }

    def render(self) -> str:
        minutes, seconds = divmod(int(self.seconds), 60)
        lines = [
            f"# Run {self.run_id}",
            "",
            f"**Task:** `{self.task}`  ",
            f"**Wall clock:** {minutes}m {seconds}s  ",
            f"**Turns:** {len(self.turns)} (SDK counted {self.sdk_turns})  ",
            f"**Cost:** ${self.cost_usd:.4f}  ",
            f"**Ended because:** {self.end_reason or '(not recorded)'}",
            "",
            "## Request",
            "",
            "```",
            self.request.strip(),
            "```",
            "",
        ]

        if self.metadata:
            lines += ["## Context", ""]
            lines += [f"- **{k}:** {v}" for k, v in self.metadata.items()]
            lines.append("")

        lines += ["## Outcome", ""]
        if self.pr_urls:
            lines += [f"- Pull request: {url}" for url in self.pr_urls]
        else:
            lines.append("- **No pull request was opened.** The run did not reach its goal.")
        lines.append("")

        if self.rounds:
            lines += [
                "## Fix-forward rounds",
                "",
                "| Round | Agent | Turns | Cost | CI verdict | CI wait | Failed checks |",
                "|---|---|---|---|---|---|---|",
            ]
            for r in self.rounds:
                lines.append(
                    f"| {r.get('round')} ({r.get('kind')}) | {r.get('agent_seconds', 0):.0f}s "
                    f"| {r.get('sdk_turns', 0)} | ${r.get('cost_usd', 0):.4f} "
                    f"| {r.get('checks', '-')} | {r.get('checks_seconds', 0):.0f}s "
                    f"| {', '.join(r.get('failed_checks', [])) or '-'} |"
                )
            lines.append("")

        if self.tool_counts:
            lines += ["## Tool use", "", "| Tool | Calls |", "|---|---|"]
            for name, count in sorted(self.tool_counts.items(), key=lambda kv: -kv[1]):
                lines.append(f"| `{name}` | {count} |")
            lines.append("")

        lines += ["## Turn timings", "", "| # | Seconds | Tools |", "|---|---|---|"]
        for turn in self.turns:
            tools = ", ".join(f"`{t}`" for t in dict.fromkeys(turn.tool_calls)) or "-"
            lines.append(f"| {turn.index} | {turn.seconds:.1f} | {tools} |")
        lines.append("")

        lines += ["## Policy refusals", ""]
        if not self.denials:
            lines.append("None. The agent stayed inside the tool surface it was given.")
        else:
            lines.append(
                "Each of these is a place the agent tried to do something the policy "
                "refused. A refusal that recurs is usually a runbook problem, not an "
                "agent problem."
            )
            lines.append("")
            for denial in self.denials:
                d = vars(denial) if hasattr(denial, "__dict__") else denial
                lines += [
                    f"- **{d.get('tool')}** — `{str(d.get('detail'))[:120]}`",
                    f"  - {d.get('reason')}",
                ]
        lines.append("")

        if self.errors:
            lines += ["## Errors", ""] + [f"- {e}" for e in self.errors] + [""]

        return "\n".join(lines)
