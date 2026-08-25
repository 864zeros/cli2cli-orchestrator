# orchestrator — multi-CLI workflow engine

The middle of the three-piece stack. A **standalone** engine that coordinates
multiple real CLI sessions into a workflow using a **typed conversation protocol**,
driving them through the [`cli2cli-mcp`](../cli2cli-mcp) substrate. Independently
valuable: it runs from its own CLI with no AOE / Telegram.

```
  front-ends (AOE, cron, CLI)  ──►  orchestrator  ──►  cli2cli-mcp  ──►  claude / gemini / shell
        (publish tasks)              (this)             (substrate)
```

## The three pieces
- **`cli2cli-mcp`** — pure substrate: drive any interactive CLI over MCP.
- **`orchestrator`** (here) — the engine: bus + conversation schema + workflow runtime.
- **`cli2cli-aoe`** — the appliance: phone/HITL/audit/persistence, a front-end on this.

Each stands alone; they compose through the **conversation contract** (`schema.py`).

## Parts
| File | Role |
|------|------|
| `schema.py` | The conversation contract — performatives + typed conversation state machines (`delegate`, `gate`), extensible via `register()`. Every type accepts universal failure performatives so a flaky CLI can't hang a conversation. |
| `bus.py` | Append-only SQLite event log = message bus + audit trail + training corpus. Validates each message against the conversation's current state before appending. |
| `client.py` | Persistent MCP client to `cli2cli-mcp`. |
| `engine.py` | Workflow runtime: runs `delegate`/`gate` steps, passes artifacts between sessions via the run dir, publishes every move to the bus. |
| `cli.py` | Standalone entry point. |

## Conversation types (v0.1)
- **`delegate`**: `START —task→ AWAITING_RESULT —result→ DONE` (or `—error→ FAILED`)
- **`gate`**: `START —propose→ AWAITING_DECISION —approve→ APPROVED / —reject→ REJECTED / —revise→ START`

New types are declared as data and `register()`-ed — the catalog is extensible.

## Workflows
A workflow is JSON: an ordered list of `delegate` and `gate` steps. Steps pass data
through the shared local filesystem — a prompt references `{artifact.txt}` and the
engine inlines that file (written by an earlier step's `out`). See `workflows/demo.json`.

## Run
```sh
py -m venv venv && .\venv\Scripts\pip install -r requirements.txt
# build the substrate first:  cd ..\cli2cli-mcp && npm install && npm run build

.\venv\Scripts\python cli.py run workflows\demo.json --yes      # auto-approve gates
.\venv\Scripts\python cli.py run workflows\demo.json            # decide gates on console
.\venv\Scripts\python cli.py types                              # list conversation types
```

`demo.json` runs two Claude sessions with a gate between them: the first describes
Fibonacci → artifact → (gate) → the second uses that artifact to write a code file.

## Status
v0.1 — sequential workflows, `delegate` + `gate`, filesystem artifact passing, SQLite
bus. Verified end-to-end (standalone). Next: parallel/fan-out steps, the `critique`
loop type, a participation-MCP so worker agents emit typed messages natively, and the
AOE re-plumbed to publish tasks onto this bus.
