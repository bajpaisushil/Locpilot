"""Catalog HTTP handlers (framework-agnostic)."""

from __future__ import annotations

from catalog.pagination import paginate
from catalog.store import all_items


def parse_int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except ValueError:
        return default


def list_items(query: dict[str, str]) -> dict:
    """GET /items?page=<n>&per_page=<n>"""
    page = parse_int(query.get("page"), 1)
    per_page = parse_int(query.get("per_page"), 10)
    return paginate(all_items(), page, per_page)
