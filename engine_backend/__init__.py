"""The commerce-agents backends implemented over the StateSet iCommerce engine."""

from pathlib import Path

UPSTREAM_ROOT = Path(__file__).resolve().parent.parent / "vendor" / "commerce-agents"

_SKILL_DIRS = {
    "shopping": UPSTREAM_ROOT / "shopping-agent" / "skills",
    "merchant": UPSTREAM_ROOT / "merchant-agent" / "skills",
}


def SKILLS_DIR(role: str) -> Path:
    """The pinned upstream skills directory for a role. Read, never copied."""
    try:
        return _SKILL_DIRS[role]
    except KeyError:
        raise ValueError(f"unknown role {role!r}; expected one of {sorted(_SKILL_DIRS)}") from None


__all__ = ["UPSTREAM_ROOT", "SKILLS_DIR"]
