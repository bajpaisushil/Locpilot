"""In-memory product store."""


def all_items() -> list[dict]:
    return [{"id": i, "name": f"Product {i}"} for i in range(1, 46)]
