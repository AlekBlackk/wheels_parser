import unittest

import betboom_web_parser as parser


class ApiStatusTests(unittest.TestCase):
    def test_marks_ended_wheel_expired(self):
        self.assertEqual(parser._api_info_to_status({"is_ended": True}), "expired")

    def test_marks_early_wheel_soon(self):
        self.assertEqual(
            parser._api_info_to_status({"is_ended": False, "is_early": True}),
            "soon",
        )

    def test_marks_running_wheel_active_regardless_of_join_state(self):
        self.assertEqual(
            parser._api_info_to_status(
                {"is_ended": False, "is_early": False, "is_joined": True}
            ),
            "active",
        )

    def test_rejects_incomplete_info(self):
        self.assertEqual(parser._api_info_to_status({"is_ended": False}), "unknown")
