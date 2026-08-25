"""
engine.py — the workflow runtime.

Runs a workflow: an ordered list of steps, each a typed conversation.
- `delegate` steps drive a real CLI session (via cli2cli) and write the result to
  an artifact file in the run dir.
- `gate` steps pause for a decision from an injected `decider` (console, Telegram,
  auto — transport-agnostic).

Steps pass data through the shared local filesystem (the run dir): a prompt may
reference `{artifact.txt}` and the engine inlines that file's contents. Every
conversation move is published to the bus (audit + state).
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import uuid
from typing import Awaitable, Callable

from bus import Bus
from client import Cli2CliClient
import schema

_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|[\x00-\x08\x0b\x0c\x0e-\x1f]"
)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


# decider(proposal_text) -> "approve" | "reject"
Decider = Callable[[str], Awaitable[str]]


async def auto_approve(_proposal: str) -> str:
    return schema.APPROVE


class WorkflowRunner:
    def __init__(
        self,
        bus: Bus,
        client: Cli2CliClient,
        *,
        run_dir: str | None = None,
        decider: Decider = auto_approve,
        working_dir: str | None = None,
        poll_interval: float = 0.5,
        timeout_s: float = 300.0,
    ) -> None:
        self.bus = bus
        self.client = client
        self.run_dir = run_dir or tempfile.mkdtemp(prefix="orch_run_")
        self.decider = decider
        self.working_dir = working_dir
        self.poll_interval = poll_interval
        self.timeout_s = timeout_s

    def _resolve(self, prompt: str) -> str:
        """Inline {artifact} references with the contents of run_dir/<artifact>."""
        def sub(m: re.Match) -> str:
            name = m.group(1)
            path = os.path.join(self.run_dir, name)
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as fh:
                    return fh.read()
            return m.group(0)  # leave untouched if not an artifact

        return re.sub(r"\{([\w.\-]+)\}", sub, prompt)

    async def _drive(self, cli: str, prompt: str) -> tuple[str, int | None]:
        """Spawn a CLI session, feed the prompt, drain output to completion."""
        import asyncio

        if cli == "claude":
            # Autonomous, prompt via stdin (multi-line safe) — mirrors the appliance.
            tmp = tempfile.mkdtemp(prefix="orch_c_")
            ppath = os.path.join(tmp, "p.txt")
            bpath = os.path.join(tmp, "run.bat")
            with open(ppath, "w", encoding="utf-8") as fh:
                fh.write(prompt)
            with open(bpath, "w", encoding="ascii") as fh:
                fh.write("@echo off\r\n")
                fh.write(f'call claude --print --dangerously-skip-permissions < "{ppath}"\r\n')
            spawn = await self.client.spawn(
                "custom", command="cmd.exe", args=["/c", bpath], working_dir=self.working_dir
            )
            cleanup = tmp
        elif cli == "shell":
            spawn = await self.client.spawn("shell", initial_prompt=prompt, working_dir=self.working_dir)
            cleanup = None
        else:
            spawn = await self.client.spawn(
                cli, initial_prompt=prompt, working_dir=self.working_dir
            )
            cleanup = None

        sid = spawn["session_id"]
        buf: list[str] = []
        exit_code: int | None = None
        deadline = asyncio.get_event_loop().time() + self.timeout_s
        try:
            while True:
                if asyncio.get_event_loop().time() > deadline:
                    await self.client.terminate(sid)
                    break
                await asyncio.sleep(self.poll_interval)
                frame = await self.client.read(sid)
                if frame.get("output"):
                    buf.append(frame["output"])
                if frame.get("is_exited"):
                    exit_code = frame.get("exit_code")
                    break
        finally:
            if cleanup:
                shutil.rmtree(cleanup, ignore_errors=True)
        return strip_ansi("".join(buf)).strip(), exit_code

    async def run(self, workflow: dict) -> dict:
        wf_name = workflow.get("name", "workflow")
        results: list[dict] = []
        for step in workflow["steps"]:
            stype = step["type"]
            conv_id = f"{wf_name}:{step['id']}:{uuid.uuid4().hex[:6]}"

            if stype == "delegate":
                prompt = self._resolve(step["prompt"])
                await self.bus.publish(
                    schema.Message(conv_id, "delegate", schema.TASK, "engine",
                                   step.get("cli", "claude"), {"instruction": prompt})
                )
                output, code = await self._drive(step.get("cli", "claude"), prompt)
                status = "ok" if code in (0, None) else "error"
                out_name = step.get("out")
                artifact_ref = None
                if out_name:
                    artifact_ref = os.path.join(self.run_dir, out_name)
                    with open(artifact_ref, "w", encoding="utf-8") as fh:
                        fh.write(output)
                perf = schema.RESULT if status == "ok" else schema.ERROR
                await self.bus.publish(
                    schema.Message(conv_id, "delegate", perf, step.get("cli", "claude"), "engine",
                                   {"status": status, "summary": output[:500], "artifact_ref": artifact_ref})
                )
                results.append({"id": step["id"], "status": status, "exit_code": code, "artifact": artifact_ref})
                if status == "error":
                    return {"workflow": wf_name, "status": "FAILED_AT_" + step["id"], "steps": results}

            elif stype == "gate":
                proposal = self._resolve(step.get("proposal", "Proceed?"))
                await self.bus.publish(
                    schema.Message(conv_id, "gate", schema.PROPOSE, "engine", "operator",
                                   {"proposal": proposal})
                )
                decision = await self.decider(proposal)
                await self.bus.publish(
                    schema.Message(conv_id, "gate", decision, "operator", "engine", {})
                )
                results.append({"id": step["id"], "decision": decision})
                if decision == schema.REJECT:
                    return {"workflow": wf_name, "status": "REJECTED_AT_" + step["id"], "steps": results}

            else:
                raise ValueError(f"unknown step type: {stype!r}")

        return {"workflow": wf_name, "status": "COMPLETED", "run_dir": self.run_dir, "steps": results}
