"""Regression test for the four-character password policy."""
import unittest
from core import InputError
from server import hash_password, parse_bulk_users, verify_password

class PasswordMinimumTests(unittest.TestCase):
    def test_four_accepted_three_rejected(self):
        self.assertTrue(verify_password("abcd", hash_password("abcd")))
        self.assertEqual(
            parse_bulk_users("worker1,1234"), [("worker1", None, "1234")]
        )
        with self.assertRaises(InputError):
            hash_password("abc")
        with self.assertRaises(InputError):
            parse_bulk_users("worker1,abc")
