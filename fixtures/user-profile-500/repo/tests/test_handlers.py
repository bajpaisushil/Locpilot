import unittest

from api.handlers import get_user_profile


class ProfileHandlerTest(unittest.TestCase):
    def test_profile_for_user_with_address(self):
        status, body = get_user_profile(1)
        self.assertEqual(status, 200)
        self.assertEqual(body["location"], {"city": "London", "country": "UK"})

    def test_unknown_user_is_404(self):
        status, _ = get_user_profile(999)
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
