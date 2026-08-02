import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from wheelsparser import betboom, config


def running_info(**extra):
    """info запущенного колеса: старт минуту назад, розыгрыш идёт полчаса."""
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    return {
        "is_ended": False,
        "is_early": False,
        "start_dttm": started.isoformat().replace("+00:00", "Z"),
        "duration_min": 30,
        **extra,
    }


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
            betboom.api_info_to_status(running_info(is_joined=True)),
            "active",
        )

    def test_marks_wheel_without_start_time_soon(self):
        # Стример создал колесо, но не запустил: API отдаёт info без
        # start_dttm, на сайте «Акция скоро начнётся» и кнопки участия нет.
        self.assertEqual(
            betboom.api_info_to_status(
                {"is_ended": False, "is_early": False, "duration_min": 30}
            ),
            "soon",
        )

    def test_marks_wheel_with_future_start_soon(self):
        start = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.assertEqual(
            betboom.api_info_to_status({
                "is_ended": False,
                "is_early": False,
                "start_dttm": start.isoformat().replace("+00:00", "Z"),
                "duration_min": 30,
            }),
            "soon",
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
            "info": running_info(),
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


class ReferralDetectionTests(unittest.TestCase):
    def test_detects_referral_from_api_description(self):
        info = {
            "title": "AUNKERE КОЛЕСО ФРИБЕТОВ",
            "description": "Розыгрыш фрибетов для рефералов",
        }
        self.assertTrue(
            betboom.is_referral_wheel("https://betboom.ru/freestream/aunkere", info)
        )

    def test_detects_referral_from_url_slug_without_info(self):
        self.assertTrue(
            betboom.is_referral_wheel(
                "https://betboom.ru/freestream/aunkereref", None
            )
        )

    def test_regular_wheel_is_not_referral(self):
        info = {
            "title": "ZONER КОЛЕСО ФРИБЕТОВ TG",
            "description": "УЧАСТВУЙ В РОЗЫГРЫШЕ ФРИБЕТОВ",
        }
        self.assertFalse(
            betboom.is_referral_wheel("https://betboom.ru/freestream/zonertg4", info)
        )

    def test_detects_referral_from_post_text(self):
        # Стример не написал про рефералов в описании колеса, а в посте —
        # написал: без сигнала из поста колесо осталось бы непомеченным.
        info = {"title": "КОЛЕСО ФРИБЕТОВ", "description": "УЧАСТВУЙ"}
        self.assertTrue(
            betboom.is_referral_wheel(
                "https://betboom.ru/freestream/aunkere",
                info,
                "Колесо для рефов 🔥 https://betboom.ru/freestream/aunkere",
            )
        )

    def test_post_text_without_referral_word_does_not_mark_wheel(self):
        info = {"title": "КОЛЕСО ФРИБЕТОВ", "description": "УЧАСТВУЙ"}
        self.assertFalse(
            betboom.is_referral_wheel(
                "https://betboom.ru/freestream/zoner", info, "КОЛЕСО ФРИБЕТА ❤️"
            )
        )

    def test_ignores_ref_inside_longer_word(self):
        info = {"title": "", "description": "префикс не считается"}
        self.assertFalse(
            betboom.is_referral_wheel("https://betboom.ru/freestream/zoner", info)
        )


class WheelDeadlineTests(unittest.TestCase):
    """Дедлайн (start_dttm + duration_min) показывается человеку, поэтому
    считается по тем же правилам, что и статус."""

    def test_end_time_is_start_plus_duration_in_msk(self):
        start = datetime(2026, 7, 31, 18, 10, tzinfo=timezone.utc)
        ends_at = betboom.wheel_ends_at({
            "start_dttm": start.isoformat().replace("+00:00", "Z"),
            "duration_min": 30,
        })
        # 18:10 UTC + 30 мин = 18:40 UTC = 21:40 МСК.
        self.assertEqual(ends_at, "2026-07-31T21:40:00+03:00")

    def test_end_time_is_empty_without_usable_start(self):
        self.assertEqual(betboom.wheel_ends_at({"duration_min": 30}), "")
        self.assertEqual(betboom.wheel_ends_at(None), "")
        # Наивная метка: неизвестно, чьё это время — окно не считаем.
        self.assertEqual(
            betboom.wheel_ends_at(
                {"start_dttm": "2026-07-31T18:10:00", "duration_min": 30}
            ),
            "",
        )


class PrecheckWheelTests(unittest.TestCase):
    def test_precheck_returns_status_and_referral_flag(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "code": 200,
            "status": "OK",
            "info": running_info(
                title="КОЛЕСО",
                description="Розыгрыш для рефералов",
            ),
        }
        session = Mock()
        session.post.return_value = response

        status, referral, ends_at = betboom.precheck_wheel(
            "https://betboom.ru/freestream/plainslug", session
        )

        self.assertEqual(status, "active")
        self.assertTrue(referral)
        self.assertTrue(ends_at)

    def test_precheck_marks_referral_by_post_text(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            "code": 200,
            "status": "OK",
            "info": running_info(title="КОЛЕСО", description="УЧАСТВУЙ"),
        }
        session = Mock()
        session.post.return_value = response

        _status, referral, _ends_at = betboom.precheck_wheel(
            "https://betboom.ru/freestream/plainslug",
            session,
            post_text="Колесо для рефов",
        )

        self.assertTrue(referral)

    def test_precheck_falls_back_to_slug_when_api_fails(self):
        session = Mock()
        session.post.side_effect = OSError("down")

        status, referral, ends_at = betboom.precheck_wheel(
            "https://betboom.ru/freestream/someref", session
        )

        self.assertEqual(status, "unknown")
        self.assertTrue(referral)
        self.assertEqual(ends_at, "")


class ExpiredCacheTests(unittest.TestCase):
    """Кэш expired не должен переживать EXPIRED_CACHE_TTL_SECONDS (короткий,
    НЕ REALERT_COOLDOWN_MINUTES): колёса BetBoom живут на постоянных адресах,
    и то же самое «истёкшее» колесо может быть перезапущено новым постом
    считаные минуты спустя — долгий TTL молча скрывал бы такой перезапуск."""

    def setUp(self):
        betboom._expired_cache.clear()
        self.addCleanup(betboom._expired_cache.clear)

    def _expired_response(self):
        return Mock(
            status_code=200,
            json=Mock(return_value={"info": {"is_ended": True}}),
        )

    def _active_response(self):
        return Mock(
            status_code=200,
            json=Mock(return_value={"info": running_info()}),
        )

    def test_expired_status_is_served_from_cache_within_cooldown(self):
        session = Mock()
        session.post.return_value = self._expired_response()
        base = datetime(2026, 1, 1, 12, 0, tzinfo=config.MSK_TZ)

        with patch("wheelsparser.betboom.now_msk", return_value=base):
            status, *_ = betboom.precheck_wheel(
                "https://betboom.ru/freestream/staya", session
            )
        self.assertEqual(status, "expired")

        # Колесо перезапущено (API теперь отдал бы active), но кэш ещё не
        # истёк — запрос к API вообще не должен уйти.
        session.post.reset_mock()
        session.post.return_value = self._active_response()
        within_ttl = timedelta(seconds=config.EXPIRED_CACHE_TTL_SECONDS - 10)
        with patch("wheelsparser.betboom.now_msk", return_value=base + within_ttl):
            status, *_ = betboom.precheck_wheel(
                "https://betboom.ru/freestream/staya", session
            )
        self.assertEqual(status, "expired")
        session.post.assert_not_called()

    def test_expired_cache_forgets_after_cooldown_window(self):
        session = Mock()
        session.post.return_value = self._expired_response()
        base = datetime(2026, 1, 1, 12, 0, tzinfo=config.MSK_TZ)

        with patch("wheelsparser.betboom.now_msk", return_value=base):
            betboom.precheck_wheel("https://betboom.ru/freestream/staya", session)

        # Колесо перезапущено на том же адресе после конца TTL кэша: кэш
        # обязан протухнуть и уйти за свежим статусом в API, а не молчать
        # ещё REALERT_COOLDOWN_MINUTES (кэш от него не зависит).
        session.post.reset_mock()
        session.post.return_value = self._active_response()
        after_ttl = base + timedelta(seconds=config.EXPIRED_CACHE_TTL_SECONDS + 1)
        with patch("wheelsparser.betboom.now_msk", return_value=after_ttl):
            status, *_ = betboom.precheck_wheel(
                "https://betboom.ru/freestream/staya", session
            )
        self.assertEqual(status, "active")
        session.post.assert_called_once()

    def test_precheck_use_cache_false_bypasses_cache(self):
        # retry_expired_links передаёт use_cache=False: его смысл в честной
        # перепроверке, а не в ожидании TTL (см. config.EXPIRED_CACHE_TTL_SECONDS).
        session = Mock()
        session.post.return_value = self._expired_response()
        base = datetime(2026, 1, 1, 12, 0, tzinfo=config.MSK_TZ)

        with patch("wheelsparser.betboom.now_msk", return_value=base):
            status, *_ = betboom.precheck_wheel(
                "https://betboom.ru/freestream/staya", session
            )
        self.assertEqual(status, "expired")

        # Кэш ещё свежий, но use_cache=False обязан всё равно уйти в API.
        session.post.reset_mock()
        session.post.return_value = self._active_response()
        with patch("wheelsparser.betboom.now_msk", return_value=base):
            status, *_ = betboom.precheck_wheel(
                "https://betboom.ru/freestream/staya", session, use_cache=False
            )
        self.assertEqual(status, "active")
        session.post.assert_called_once()

    def test_classify_wheels_also_respects_cache_ttl(self):
        session_calls = []

        def fake_build_session():
            session = Mock()
            session_calls.append(session)
            session.post.return_value = self._expired_response()
            return session

        base = datetime(2026, 1, 1, 12, 0, tzinfo=config.MSK_TZ)
        item = {"url": "https://betboom.ru/freestream/staya"}

        with (
            patch("wheelsparser.betboom.build_session", fake_build_session),
            patch("wheelsparser.betboom.now_msk", return_value=base),
        ):
            active_items, _soon, _unknown = betboom.classify_wheels([item])
        self.assertEqual(active_items, [])

        def fake_build_session_active():
            session = Mock()
            session.post.return_value = self._active_response()
            return session

        after_ttl = base + timedelta(seconds=config.EXPIRED_CACHE_TTL_SECONDS + 1)
        with (
            patch("wheelsparser.betboom.build_session", fake_build_session_active),
            patch("wheelsparser.betboom.now_msk", return_value=after_ttl),
        ):
            active_items, _soon, _unknown = betboom.classify_wheels([item])
        self.assertEqual(active_items, [item])


class RegressionTests(unittest.TestCase):
    def test_active_check_does_not_require_playwright(self):
        self.assertFalse(hasattr(betboom, "async_playwright"))

    def test_active_max_age_is_twenty_hours_by_default(self):
        self.assertEqual(config.ACTIVE_MAX_AGE_HOURS, 20)


if __name__ == "__main__":
    unittest.main()
