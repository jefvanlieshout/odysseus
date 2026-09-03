"""Tool-visibility broker contract for the assistant fork.

This module deliberately does NOT execute tools and does NOT grant permission.
It only decides which already-permitted tool schemas are useful to expose to a
model on a turn.  That preserves the controller as the authority boundary.

The first production integration will replace layered keyword/regex gating with
this typed state model incrementally; v0.2.0 only establishes and tests the
contract so the fork foundation does not change live behavior yet.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Mapping, Sequence


class ToolRisk(str, Enum):
    READ_ONLY = "read_only"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"
    PRIVILEGED = "privileged"


@dataclass(frozen=True, slots=True)
class ToolDescriptor:
    name: str
    capabilities: frozenset[str]
    risk: ToolRisk = ToolRisk.READ_ONLY
    source: str = "builtin"
    core_visible: bool = False
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name must not be empty")
        if not self.capabilities:
            raise ValueError(f"tool {self.name!r} must declare at least one capability")


@dataclass(slots=True)
class ConversationToolState:
    """Explicit conversational capability state; no magic-word persistence."""

    active_capabilities: set[str] = field(default_factory=set)
    last_tool_names: list[str] = field(default_factory=list)

    def activate(self, *capabilities: str) -> None:
        self.active_capabilities.update(c.strip() for c in capabilities if c.strip())

    def note_tool_use(self, tool: ToolDescriptor, *, keep_last: int = 8) -> None:
        self.active_capabilities.update(tool.capabilities)
        self.last_tool_names.append(tool.name)
        if len(self.last_tool_names) > keep_last:
            del self.last_tool_names[:-keep_last]

    def clear(self) -> None:
        self.active_capabilities.clear()
        self.last_tool_names.clear()


@dataclass(frozen=True, slots=True)
class ToolSelection:
    visible: tuple[str, ...]
    omitted_permitted: tuple[str, ...]
    reasons: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ToolCandidate:
    """One relevance signal proposed to the Broker.

    Providers may emit multiple signals for the same tool. The Broker keeps the
    strongest signal after enforcing the controller-supplied permission set.
    """

    name: str
    tier: int
    score: float = 0.0
    reason: str = "candidate"


class ToolBroker:
    """Select visibility *within* a controller-supplied permission set.

    Inputs such as semantic scores or router suggestions are recommendations.
    They can never make a non-permitted tool visible.
    """

    def __init__(self, descriptors: Iterable[ToolDescriptor], *, max_visible: int = 16):
        self._tools = {t.name: t for t in descriptors}
        if len(self._tools) == 0:
            raise ValueError("at least one tool descriptor is required")
        if max_visible < 1:
            raise ValueError("max_visible must be >= 1")
        self.max_visible = max_visible

    def descriptor(self, name: str) -> ToolDescriptor | None:
        return self._tools.get(name)

    def discover(
        self,
        *,
        permitted_names: set[str],
        capabilities: set[str] | None = None,
    ) -> tuple[str, ...]:
        """Recovery/discovery path when the automatic selector missed a tool."""
        caps = capabilities or set()
        out = []
        for name, tool in self._tools.items():
            if name not in permitted_names:
                continue
            if caps and not (tool.capabilities & caps):
                continue
            out.append(name)
        return tuple(sorted(out))

    def select_candidates(
        self,
        *,
        permitted_names: set[str],
        candidates: Iterable[ToolCandidate],
    ) -> ToolSelection:
        """Select from provider-produced candidates within controller permission."""
        ranked: dict[str, tuple[int, float, str]] = {}

        for candidate in candidates:
            name = str(candidate.name or "").strip()
            if name not in permitted_names or name not in self._tools:
                continue
            value = (
                int(candidate.tier),
                float(candidate.score),
                str(candidate.reason or "candidate"),
            )
            current = ranked.get(name)
            if current is None or (value[0], value[1]) > (current[0], current[1]):
                ranked[name] = value

        ordered = sorted(
            ranked.items(),
            key=lambda item: (-item[1][0], -item[1][1], item[0]),
        )
        visible = tuple(name for name, _ in ordered[: self.max_visible])
        reasons = {name: ranked[name][2] for name in visible}
        permitted_known = sorted(
            name for name in permitted_names if name in self._tools
        )
        omitted = tuple(
            name for name in permitted_known if name not in visible
        )
        return ToolSelection(
            visible=visible,
            omitted_permitted=omitted,
            reasons=reasons,
        )

    def select(
        self,
        *,
        permitted_names: set[str],
        state: ConversationToolState,
        suggested_capabilities: Sequence[str] = (),
        semantic_scores: Mapping[str, float] | None = None,
        forced_names: Sequence[str] = (),
    ) -> ToolSelection:
        """Compatibility selector using the original v0.2.x signal contract."""
        semantic_scores = semantic_scores or {}
        suggested = {c for c in suggested_capabilities if c}
        sticky = set(state.active_capabilities)
        candidates: list[ToolCandidate] = []

        for tool in self._tools.values():
            if tool.core_visible:
                candidates.append(
                    ToolCandidate(tool.name, 100, 0.0, "core-visible")
                )

        for name in forced_names:
            candidates.append(
                ToolCandidate(str(name), 95, 0.0, "explicit-context")
            )

        for tool in self._tools.values():
            if tool.capabilities & sticky:
                candidates.append(
                    ToolCandidate(
                        tool.name,
                        90,
                        0.0,
                        "sticky-capability",
                    )
                )
            elif tool.capabilities & suggested:
                candidates.append(
                    ToolCandidate(
                        tool.name,
                        70,
                        0.0,
                        "router-suggestion",
                    )
                )

        for name, score in semantic_scores.items():
            candidates.append(
                ToolCandidate(
                    str(name),
                    50,
                    float(score),
                    "semantic-rank",
                )
            )

        return self.select_candidates(
            permitted_names=permitted_names,
            candidates=candidates,
        )
