import unittest

from api.handlers import get_user_profile


class ProfileAcceptance(unittest.TestCase):
    def test_user_without_address_gets_profile(self):
        status, body = get_user_profile(3)
        self.assertEqual(status, 200)
        self.assertEqual(body["name"], "Linus Torvalds")
        self.assertIsNone(body["location"])

    def test_user_with_address_unchanged(self):
        status, body = get_user_profile(2)
        self.assertEqual(status, 200)
        self.assertEqual(body["location"], {"city": "Arlington", "country": "US"})
