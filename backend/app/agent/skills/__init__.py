"""Skill registry.

The one place that knows which skills exist. The orchestrator resolves by name
and never imports a concrete skill, so adding a capability is a new module plus
one line here — the open/closed boundary that keeps routing honest.
"""

from __future__ import annotations

from .artifact import ArtifactSkill
from .base import Skill, SkillContext, SkillResult
from .grounded_qa import GroundedQASkill
from .ship30.handler import Ship30Skill

_REGISTRY: dict[str, Skill] = {
    GroundedQASkill.name: GroundedQASkill(),
    Ship30Skill.name: Ship30Skill(),
    ArtifactSkill.name: ArtifactSkill(),
}


def get_skill(name: str) -> Skill:
    """Resolve a skill by name, falling back to grounded QA.

    Falls back rather than raising: a routing miss should degrade to the safest
    capability, not fail the user's request. The fallback is logged by the
    router, so it stays visible.
    """
    return _REGISTRY.get(name, _REGISTRY[GroundedQASkill.name])


def skill_names() -> list[str]:
    return sorted(_REGISTRY)


__all__ = [
    "ArtifactSkill",
    "GroundedQASkill",
    "Ship30Skill",
    "Skill",
    "SkillContext",
    "SkillResult",
    "get_skill",
    "skill_names",
]
