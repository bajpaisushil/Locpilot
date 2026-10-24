"""Combine offers returned by several suppliers into one result list."""

from offers.models import Offer


def merge_offers(supplier_results: dict[str, list[Offer]]) -> list[Offer]:
    """Merge supplier results, keeping only the cheapest offer for each product.

    The same room can be sold by several suppliers; customers should only see
    the cheapest one.
    """
    merged: list[Offer] = []
    for supplier, offers in supplier_results.items():
        best: dict[tuple[str, str, str], Offer] = {}
        for offer in offers:
            key = offer.dedupe_key()
            if key not in best or offer.price < best[key].price:
                best[key] = offer
        merged.extend(best.values())
    return sort_offers(merged)


def sort_offers(offers: list[Offer]) -> list[Offer]:
    """Cheapest first, grouped by hotel."""
    return sorted(offers, key=lambda o: (o.hotel_id, o.price))
