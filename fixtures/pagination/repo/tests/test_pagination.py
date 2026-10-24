import unittest

from catalog.api import parse_int
from catalog.pagination import clamp_per_page


class PaginationHelpersTest(unittest.TestCase):
    def test_clamp_per_page_defaults(self):
        self.assertEqual(clamp_per_page(None), 10)
        self.assertEqual(clamp_per_page(0), 10)
        self.assertEqual(clamp_per_page(500), 100)

    def test_parse_int(self):
        self.assertEqual(parse_int("3", 1), 3)
        self.assertEqual(parse_int("x", 1), 1)
        self.assertEqual(parse_int(None, 7), 7)


if __name__ == "__main__":
    unittest.main()
