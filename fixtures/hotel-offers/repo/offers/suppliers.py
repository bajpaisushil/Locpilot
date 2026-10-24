"""Supplier clients. In production these call partner APIs; here they return canned data."""

from offers.models import Offer

_CATALOG = {
    "bedsonline": [
        Offer("H1", "DBL", "BB", "bedsonline", 120.0),
        Offer("H1", "TWN", "RO", "bedsonline", 95.0),
        Offer("H2", "DBL", "RO", "bedsonline", 80.0),
    ],
    "roomhub": [
        Offer("H1", "DBL", "BB", "roomhub", 110.0),
        Offer("H2", "SGL", "RO", "roomhub", 60.0),
    ],
}


def fetch_offers(supplier: str, city: str) -> list[Offer]:
    """Return the offers a supplier has for a city (canned data, deterministic)."""
    if city.lower() != "lisbon":
        return []
    return list(_CATALOG.get(supplier, []))


def available_suppliers() -> list[str]:
    return sorted(_CATALOG)
