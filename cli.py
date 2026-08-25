"""
cli.py — standalone entry point for the orchestrator.

    python cli.py run <workflow.json> [--yes] [--cwd DIR]

Runs a multi-CLI workflow with typed conversations, entirely independent of the
AOE appliance (no Telegram). Gates are decided on the console unless --yes.

    python cli.py types           # list registered conversation types
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from bus import Bus
from client import Cli2CliClient
from engine import WorkflowRunner, auto_approve
import schema

_here = os.path.dirname(os.path.abspath(__file__))
# cli2cli-mcp is a standalone sibling at the dev root. One level up from
# orchestrator/ = C:\dev; override with CLI2CLI_SERVER_JS.
DEFAULT_SERVER_JS = os.path.join(
    os.path.dirname(_here), "cli2cli-mcp", "dist", "index.js"
)


async def _console_decider(proposal: str) -> str:
    print("\n--- GATE: decision required ---")
    print(proposal)
    ans = await asyncio.to_thread(input, "approve / reject ? [a/r]: ")
    return schema.APPROVE if ans.strip().lower().startswith("a") else schema.REJECT


async def run_workflow(path: str, auto: bool, cwd: str | None) -> int:
    with open(path, encoding="utf-8") as fh:
        workflow = json.load(fh)

    server_js = os.getenv("CLI2CLI_SERVER_JS") or DEFAULT_SERVER_JS
    if not os.path.isfile(server_js):
        print(f"cli2cli-mcp not built at {server_js} (run: cd ../cli2cli-mcp && npm run build)")
        return 2

    bus = Bus()
    await bus.init()
    client = Cli2CliClient(server_js)
    await client.connect()
    try:
        runner = WorkflowRunner(
            bus,
            client,
            decider=auto_approve if auto else _console_decider,
            working_dir=cwd,
        )
        print(f"running workflow '{workflow.get('name')}' (run_dir={runner.run_dir})")
        result = await runner.run(workflow)
    finally:
        await client.close()

    print("\n=== RESULT ===")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "COMPLETED" else 1


def main() -> None:
    ap = argparse.ArgumentParser(prog="orchestrate")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a workflow json")
    r.add_argument("workflow")
    r.add_argument("--yes", action="store_true", help="auto-approve all gates")
    r.add_argument("--cwd", default=None, help="working directory for CLI sessions")
    sub.add_parser("types", help="list registered conversation types")

    args = ap.parse_args()
    if args.cmd == "types":
        print("registered conversation types:", ", ".join(schema.catalog()))
        return
    if args.cmd == "run":
        sys.exit(asyncio.run(run_workflow(args.workflow, args.yes, args.cwd)))


if __name__ == "__main__":
    main()
