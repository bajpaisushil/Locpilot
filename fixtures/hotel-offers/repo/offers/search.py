"""Hotel search entry point used by the web API."""

from __future__ import annotations

from offers.aggregator import merge_offers
from offers.models import Offer
from offers.suppliers import available_suppliers, fetch_offers


def search_hotels(city: str, suppliers: list[str] | None = None) -> list[Offer]:
    """Query every supplier for the city and return the merged, de-duplicated offers."""
    names = suppliers or available_suppliers()
    results = {name: fetch_offers(name, city) for name in names}
    return merge_offers(results)


def search_response(city: str) -> dict:
    """JSON-serialisable payload returned by GET /search?city=..."""
    offers = search_hotels(city)
    return {
        "city": city,
        "count": len(offers),
        "offers": [
            {"hotel": o.hotel_id, "room": o.room_type, "rate": o.rate_plan, "price": o.price, "supplier": o.supplier}
            for o in offers
        ],
    }
