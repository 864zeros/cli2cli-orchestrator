"""
bus.py — the event/message bus, backed by an append-only SQLite log.

Every message published becomes a durable row. That single store is simultaneously
the message bus, the audit trail, and the training corpus. Consumers read messages
since a cursor; conversation state is derived by replaying a conversation's messages
through its ConversationType. (Swap SQLite for Redis/NATS later without changing the
message contract.)
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime, timezone

import aiosqlite

from schema import Message, get as get_conversation

DEFAULT_DB = os.path.join(os.path.expanduser("~"), ".agent_bridge", "orchestrator_bus.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL,
    conversation_id  TEXT NOT NULL,
    conversation_type TEXT NOT NULL,
    performative     TEXT NOT NULL,
    sender           TEXT NOT NULL,
    recipient        TEXT NOT NULL,
    payload          TEXT NOT NULL,
    correlation_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_conv ON events(conversation_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Bus:
    def __init__(self, db_path: str = DEFAULT_DB) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        async with aiosqlite.connect(self.db_path) as db:
            await db.executescript(_SCHEMA)
            await db.commit()

    async def publish(self, msg: Message) -> int:
        """Validate against the conversation's current state, then append."""
        state = await self.conversation_state(msg.conversation_id, msg.conversation_type)
        msg.validate_against(get_conversation(msg.conversation_type), state)
        async with aiosqlite.connect(self.db_path) as db:
            cur = await db.execute(
                """INSERT INTO events
                   (ts, conversation_id, conversation_type, performative, sender, recipient, payload, correlation_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    _now(),
                    msg.conversation_id,
                    msg.conversation_type,
                    msg.performative,
                    msg.sender,
                    msg.recipient,
                    json.dumps(msg.payload),
                    msg.correlation_id,
                ),
            )
            await db.commit()
            return cur.lastrowid

    async def messages(self, conversation_id: str) -> list[dict]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT * FROM events WHERE conversation_id = ? ORDER BY seq", (conversation_id,)
            )
            return [dict(r) for r in rows]

    async def conversation_state(self, conversation_id: str, conversation_type: str) -> str:
        """Derive current state by replaying the conversation's performatives."""
        ct = get_conversation(conversation_type)
        state = ct.start
        for row in await self.messages(conversation_id):
            perf = row["performative"]
            if ct.legal(state, perf):
                state = ct.next_state(state, perf)
        return state

    async def read_since(self, cursor: int, recipient: str | None = None) -> list[dict]:
        """Fetch events after `cursor` (optionally addressed to `recipient`)."""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            if recipient is None:
                rows = await db.execute_fetchall(
                    "SELECT * FROM events WHERE seq > ? ORDER BY seq", (cursor,)
                )
            else:
                rows = await db.execute_fetchall(
                    "SELECT * FROM events WHERE seq > ? AND recipient = ? ORDER BY seq",
                    (cursor, recipient),
                )
            return [dict(r) for r in rows]
