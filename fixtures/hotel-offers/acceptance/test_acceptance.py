import unittest

from offers.aggregator import merge_offers
from offers.models import Offer
from offers.search import search_response


class DuplicateOffersAcceptance(unittest.TestCase):
    def test_same_room_from_two_suppliers_is_shown_once_at_cheapest_price(self):
        merged = merge_offers({
            "bedsonline": [Offer("H1", "DBL", "BB", "bedsonline", 120.0)],
            "roomhub": [Offer("H1", "DBL", "BB", "roomhub", 110.0)],
        })
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].price, 110.0)
        self.assertEqual(merged[0].supplier, "roomhub")

    def test_search_has_no_duplicate_products(self):
        payload = search_response("Lisbon")
        keys = [(o["hotel"], o["room"], o["rate"]) for o in payload["offers"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(payload["count"], 4)

    def test_distinct_products_from_different_suppliers_are_kept(self):
        merged = merge_offers({
            "a": [Offer("H1", "DBL", "BB", "a", 100.0)],
            "b": [Offer("H1", "TWN", "BB", "b", 90.0)],
        })
        self.assertEqual(len(merged), 2)
