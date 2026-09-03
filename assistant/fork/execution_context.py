"""Shared execution identity for current and future assistant agents.

The context is provenance, not authority.  Permission checks live in the
controller/tool-execution layer and must not trust an LLM-provided agent_id.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping
import uuid


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    agent_id: str = "main"
    delegated_by: str | None = None
    source: str = "unknown"
    user_id: str | None = None
    session_id: str | None = None
    conversation_id: str | None = None
    run_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    metadata: Mapping[str, str] = field(default_factory=dict)

    def child(self, agent_id: str) -> "ExecutionContext":
        """Create provenance for a future delegated agent without granting rights."""
        agent_id = agent_id.strip()
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        return ExecutionContext(
            agent_id=agent_id,
            delegated_by=self.agent_id,
            source=self.source,
            user_id=self.user_id,
            session_id=self.session_id,
            conversation_id=self.conversation_id,
            metadata=dict(self.metadata),
        )
