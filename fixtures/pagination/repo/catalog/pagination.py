"""Pagination helpers shared by the catalog API."""

from __future__ import annotations

DEFAULT_PER_PAGE = 10
MAX_PER_PAGE = 100


def clamp_per_page(per_page: int | None) -> int:
    if per_page is None or per_page <= 0:
        return DEFAULT_PER_PAGE
    return min(per_page, MAX_PER_PAGE)


def paginate(items: list, page: int, per_page: int | None = None) -> dict:
    """Return one page of ``items``. Pages are 1-based: page=1 is the first page."""
    size = clamp_per_page(per_page)
    page = max(page, 1)
    start = page * size
    end = start + size
    total_pages = (len(items) + size - 1) // size
    return {
        "items": items[start:end],
        "page": page,
        "per_page": size,
        "total": len(items),
        "total_pages": total_pages,
        "has_next": page < total_pages,
    }
