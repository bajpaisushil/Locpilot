"""Turn database records into API payloads."""


def serialize_address(address: dict) -> dict:
    return {"city": address["city"], "country": address["country"]}


def serialize_profile(user: dict) -> dict:
    return {
        "id": user["id"],
        "name": user["name"],
        "email": user["email"],
        "location": serialize_address(user["address"]),
    }
