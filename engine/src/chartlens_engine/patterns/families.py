"""Family classes shared by the definition fit and relevance (ADR-0022 §7, §15, §18)."""

from __future__ import annotations

REVERSAL_FAMILIES: frozenset[str] = frozenset(
    {"double", "triple", "head_shoulders", "rounding", "v", "wedge"}
)
"""Families whose definition reverses a prior trend (§7). Triangles, flags, pennants and
cup & handle continue one; rectangles and symmetrical triangles are neutral."""
