"""Tiny fake user database."""

from __future__ import annotations

USERS = {
    1: {"id": 1, "name": "Ada Lovelace", "email": "ada@example.com",
        "address": {"street": "12 St James's Square", "city": "London", "country": "UK"}},
    2: {"id": 2, "name": "Grace Hopper", "email": "grace@example.com",
        "address": {"street": "1 Navy Way", "city": "Arlington", "country": "US"}},
    3: {"id": 3, "name": "Linus Torvalds", "email": "linus@example.com"},
}


def find_user(user_id: int) -> dict | None:
    return USERS.get(user_id)
