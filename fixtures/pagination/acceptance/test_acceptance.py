import unittest

from catalog.api import list_items
from catalog.pagination import paginate


class PaginationAcceptance(unittest.TestCase):
    def test_first_page_starts_at_first_item(self):
        result = list_items({"page": "1", "per_page": "10"})
        self.assertEqual([i["id"] for i in result["items"]], list(range(1, 11)))
        self.assertTrue(result["has_next"])

    def test_last_page_is_partial(self):
        result = paginate(list(range(45)), 5, 10)
        self.assertEqual(result["items"], [40, 41, 42, 43, 44])
        self.assertFalse(result["has_next"])
        self.assertEqual(result["total_pages"], 5)
