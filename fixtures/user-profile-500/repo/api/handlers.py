"""HTTP handlers. Each returns (status_code, body)."""

from api.db import find_user
from api.serializers import serialize_profile


def get_user_profile(user_id: int) -> tuple[int, dict]:
    """GET /users/<id>/profile"""
    try:
        user = find_user(user_id)
        if user is None:
            return 404, {"error": "user not found"}
        return 200, serialize_profile(user)
    except Exception:  # noqa: BLE001 - framework would turn this into a 500 page
        return 500, {"error": "internal server error"}
