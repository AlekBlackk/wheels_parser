import unittest
from unittest.mock import Mock

from wheelsparser import betboom, config


class ApiStatusTests(unittest.TestCase):
    def test_marks_ended_wheel_expired(self):
        self.assertEqual(betboom.api_info_to_status({"is_ended": True}), "expired")

    def test_marks_early_wheel_soon(self):
        self.assertEqual(
            betboom.api_info_to_status({"is_ended": False, "is_early": True}),
            "soon",
        )

    def test_marks_running_wheel_active_regardless_of_join_state(self):
        self.assertEqual(
            betboom.api_info_to_status(
                {"is_ended": False, "is_early": False, "is_joined": True}
            ),
            "active",
        )

    def test_rejects_incomplete_info(self):
        self.assertEqual(betboom.api_info_to_status({"is_ended": False}), "unknown")

    def test_expires_wheel_whose_duration_has_passed(self):
        self.assertEqual(
            betboom.api_info_to_status({
                "is_ended": False,
                "is_early": False,
                "start_dttm": "2020-01-01T00:00:00Z",
                "duration_min": 30,
            }),
            "expired",
        )


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

        status = betboom.check_wheel_status(
            "https://betboom.ru/freestream/zonertg10?utm_source=test", session
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
            betboom.check_wheel_status("https://betboom.ru/freestream/a", session),
            "unknown",
        )


class RegressionTests(unittest.TestCase):
    def test_active_check_does_not_require_playwright(self):
        self.assertFalse(hasattr(betboom, "async_playwright"))

    def test_active_max_age_is_twenty_hours_by_default(self):
        self.assertEqual(config.ACTIVE_MAX_AGE_HOURS, 20)


if __name__ == "__main__":
    unittest.main()
