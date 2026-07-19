import unittest
from unittest.mock import Mock

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


class ApiCheckTests(unittest.TestCase):
    def test_api_check_posts_normalized_freestream_url(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "code": 200,
            "status": "OK",
            "info": {"is_ended": False, "is_early": False},
        }
        session = Mock()
        session.post.return_value = response

        status = parser._check_wheel_api(
            {"url": "https://betboom.ru/freestream/zonertg10?utm_source=test"},
            session,
        )

        self.assertEqual(status, "active")
        self.assertEqual(
            session.post.call_args.kwargs["json"],
            {"streamer_link": "https://betboom.ru/freestream/zonertg10"},
        )

    def test_api_check_returns_unknown_for_http_failure(self):
        response = Mock(status_code=503)
        session = Mock()
        session.post.return_value = response
        self.assertEqual(
            parser._check_wheel_api({"url": "https://betboom.ru/freestream/a"}, session),
            "unknown",
        )


class RegressionTests(unittest.TestCase):
    def test_active_check_module_does_not_require_playwright(self):
        self.assertFalse(hasattr(parser, "async_playwright"))
