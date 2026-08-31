"""
schema.py — the conversation contract (the lingua franca of the orchestrator).

Messages are typed by a PERFORMATIVE (task, result, propose, approve, ...), and a
CONVERSATION TYPE is a small state machine that enforces which performatives are
legal and what may follow what. This is the stable contract all three products
(cli2cli-mcp substrate, this orchestrator, a front-end appliance) speak, so they stay
independently valuable yet compose.

The catalog is EXTENSIBLE: new conversation types are declared as data and
registered. Two are built in — `delegate` and `gate`. Every conversation also
accepts universal FAILURE performatives (error/timeout/malformed/refuse) from any
non-terminal state, so a flaky CLI can never leave a conversation hanging.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- performatives ---------------------------------------------------------
TASK = "task"
RESULT = "result"
PROPOSE = "propose"
APPROVE = "approve"
REJECT = "reject"
REVISE = "revise"
INFORM = "inform"
# universal failure performatives — legal from any non-terminal state
ERROR = "error"
TIMEOUT = "timeout"
MALFORMED = "malformed"
REFUSE = "refuse"
FAILURE_PERFORMATIVES = {ERROR, TIMEOUT, MALFORMED, REFUSE}


@dataclass
class ConversationType:
    """A named interaction protocol = a state machine over performatives."""
    name: str
    start: str
    # transitions[state][performative] -> next_state
    transitions: dict[str, dict[str, str]]
    terminal: set[str]
    # required payload keys per performative (validated on publish)
    required: dict[str, list[str]] = field(default_factory=dict)
    # state entered when a universal failure performative fires
    failed_state: str = "FAILED"

    def legal(self, state: str, performative: str) -> bool:
        if state in self.terminal:
            return False
        if performative in FAILURE_PERFORMATIVES:
            return True
        return performative in self.transitions.get(state, {})

    def next_state(self, state: str, performative: str) -> str:
        if performative in FAILURE_PERFORMATIVES:
            return self.failed_state
        return self.transitions[state][performative]

    def is_terminal(self, state: str) -> bool:
        return state in self.terminal


# --- registry (extensible) -------------------------------------------------
_REGISTRY: dict[str, ConversationType] = {}


def register(ct: ConversationType) -> None:
    _REGISTRY[ct.name] = ct


def get(name: str) -> ConversationType:
    if name not in _REGISTRY:
        raise KeyError(f"unknown conversation type: {name!r} (have {list(_REGISTRY)})")
    return _REGISTRY[name]


def catalog() -> list[str]:
    return list(_REGISTRY)


# --- built-in conversation types ------------------------------------------
# delegate: initiator asks a worker to do something; worker returns a result.
#   START --task--> AWAITING_RESULT --result--> DONE   (or --error--> FAILED)
register(
    ConversationType(
        name="delegate",
        start="START",
        transitions={
            "START": {TASK: "AWAITING_RESULT"},
            "AWAITING_RESULT": {RESULT: "DONE"},
        },
        terminal={"DONE", "FAILED"},
        required={
            TASK: ["instruction"],
            RESULT: ["status", "summary"],  # artifact_ref optional
        },
    )
)

# gate: initiator proposes; a human/decider approves, rejects, or asks to revise.
#   START --propose--> AWAITING_DECISION --approve--> APPROVED
#                                        --reject--> REJECTED
#                                        --revise--> START
register(
    ConversationType(
        name="gate",
        start="START",
        transitions={
            "START": {PROPOSE: "AWAITING_DECISION"},
            "AWAITING_DECISION": {APPROVE: "APPROVED", REJECT: "REJECTED", REVISE: "START"},
        },
        terminal={"APPROVED", "REJECTED", "FAILED"},
        required={
            PROPOSE: ["proposal"],
            REJECT: [],
            APPROVE: [],
        },
    )
)


@dataclass
class Message:
    """The envelope. Transport-agnostic; the bus persists these verbatim."""
    conversation_id: str
    conversation_type: str
    performative: str
    sender: str
    recipient: str
    payload: dict[str, Any] = field(default_factory=dict)
    correlation_id: str | None = None

    def validate_against(self, ct: ConversationType, state: str) -> None:
        if not ct.legal(state, self.performative):
            raise ValueError(
                f"illegal move: '{self.performative}' not allowed in state "
                f"'{state}' of conversation '{ct.name}'"
            )
        for key in ct.required.get(self.performative, []):
            if key not in self.payload:
                raise ValueError(
                    f"'{self.performative}' payload missing required key '{key}'"
                )
