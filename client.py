"""
client.py — persistent MCP client for the cli2cli-mcp substrate.

The orchestrator drives real CLI sessions by calling the substrate's tools. The
MCP stdio session is held open inside ONE long-lived task (anyio scopes must open
and close in the same task), matching the pattern proven in the AOE appliance.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class Cli2CliClient:
    def __init__(self, server_js: str, node: str = "node") -> None:
        self._params = StdioServerParameters(command=node, args=[server_js], env=os.environ.copy())
        self.session: ClientSession | None = None
        self._task: asyncio.Task | None = None
        self._ready = asyncio.Event()
        self._stop = asyncio.Event()
        self._error: BaseException | None = None

    async def _run(self) -> None:
        try:
            async with stdio_client(self._params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self.session = session
                    self._ready.set()
                    await self._stop.wait()
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
            self._ready.set()

    async def connect(self) -> None:
        self._task = asyncio.create_task(self._run())
        await self._ready.wait()
        if self._error is not None:
            raise RuntimeError(f"cli2cli bridge failed to start: {self._error}") from self._error

    async def close(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    async def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self.session is not None, "not connected"
        result = await self.session.call_tool(name, args)
        text = result.content[0].text  # type: ignore[union-attr]
        if result.isError:
            raise RuntimeError(f"{name} failed: {text}")
        return json.loads(text)

    async def spawn(self, cli_type, initial_prompt="", working_dir=None, command=None, args=None):
        payload: dict[str, Any] = {"cli_type": cli_type, "initial_prompt": initial_prompt}
        if working_dir:
            payload["working_dir"] = working_dir
        if command:
            payload["command"] = command
        if args:
            payload["args"] = args
        return await self._call("spawn_cli_session", payload)

    async def read(self, session_id):
        return await self._call("read_cli_stream", {"session_id": session_id})

    async def write(self, session_id, input_text, append_newline=True):
        return await self._call(
            "write_cli_input",
            {"session_id": session_id, "input_text": input_text, "append_newline": append_newline},
        )

    async def terminate(self, session_id):
        return await self._call("terminate_cli_session", {"session_id": session_id})
