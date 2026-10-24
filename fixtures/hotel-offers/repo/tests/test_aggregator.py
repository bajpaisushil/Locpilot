import unittest

from offers.aggregator import merge_offers, sort_offers
from offers.models import Offer


class MergeOffersTest(unittest.TestCase):
    def test_keeps_cheapest_offer_within_one_supplier(self):
        offers = [
            Offer("H1", "DBL", "BB", "bedsonline", 130.0),
            Offer("H1", "DBL", "BB", "bedsonline", 120.0),
        ]
        merged = merge_offers({"bedsonline": offers})
        self.assertEqual([o.price for o in merged], [120.0])

    def test_different_products_are_kept(self):
        offers = [
            Offer("H1", "DBL", "BB", "bedsonline", 120.0),
            Offer("H1", "TWN", "BB", "bedsonline", 100.0),
        ]
        self.assertEqual(len(merge_offers({"bedsonline": offers})), 2)

    def test_sort_offers_cheapest_first_per_hotel(self):
        offers = [Offer("H2", "DBL", "RO", "a", 50.0), Offer("H1", "DBL", "RO", "a", 90.0),
                  Offer("H1", "SGL", "RO", "a", 70.0)]
        self.assertEqual([(o.hotel_id, o.price) for o in sort_offers(offers)],
                         [("H1", 70.0), ("H1", 90.0), ("H2", 50.0)])


if __name__ == "__main__":
    unittest.main()
