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


def ended_info(**extra):
    """info завершённого колеса: полный ответ API, а не заглушка.

    is_ended в одиночку статусом больше не считается (см. stub_info), поэтому
    тестам про expired нужен ответ с окном розыгрыша.
    """
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    return {
        "is_ended": True,
        "is_early": False,
        "start_dttm": started.isoformat().replace("+00:00", "Z"),
        "duration_min": 30,
        **extra,
    }


def stub_info(**extra):
    """Ответ-заглушка API BetBoom: is_ended=true и ни одного пригодного поля.

    Ровно такой ответ API отдаёт с августа 2026 на ЛЮБОЙ streamer_link,
    включая живое колесо (проверено на betboom.ru/freestream/vlazhniy) и
    заведомо несуществующий slug. start_dttm в нём — время запроса, а не
    старт розыгрыша; duration_min и is_early из ответа исчезли.
    """
    return {
        "start_dttm": "2026-08-30T14:41:39.968Z",
        "title": "Колесо Фрибетов",
        "prizes": [500],
        "is_ended": True,
        "rules_link": "https://static.mobile-bb.com/x/various_files/y.pdf",
        **extra,
    }


WHEEL_PAGE_HTML = (
    "<html><body>"
    '<script id="__NEXT_DATA__" type="application/json">'
    '{"props": {"pageProps": {"uid": "action-uid-1", "hash": "signature-1"}}}'
    "</script></body></html>"
)


def wheel_page_response():
    """Страница колеса с __NEXT_DATA__: оттуда берутся action_uid и подпись."""
    return Mock(status_code=200, text=WHEEL_PAGE_HTML)


def wheel_session(post_response=None):
    """Сессия, отдающая страницу колеса на GET и заданный ответ API на POST.

    Проверка статуса стоит двух запросов: страница (за подписью) и сам
    get-info (см. betboom._fetch_action_signature).
    """
    session = Mock()
    session.get.return_value = wheel_page_response()
    if post_response is not None:
        session.post.return_value = post_response
    return session


class ApiStatusTests(unittest.TestCase):
    def test_rejects_stub_info_without_classifiable_fields(self):
        # is_ended=true в одиночку доверия не заслуживает: заглушка API
        # отдаёт его для живых колёс, и вера в него означала бы fail-closed
        # (все находки молча пропадают). unknown уходит fail-open.
        self.assertEqual(betboom.api_info_to_status(stub_info()), "unknown")

    def test_expires_ended_wheel_with_known_window(self):
        # Настоящий ответ с окном розыгрыша: is_ended=true по-прежнему
        # означает «завершилось» — fail-open ничего здесь не смягчает.
        self.assertEqual(
            betboom.api_info_to_status({
                "is_ended": True,
                "is_early": False,
                "start_dttm": "2026-08-30T10:00:00Z",
                "duration_min": 30,
            }),
            "expired",
        )

    def test_expires_ended_wheel_with_is_early_flag(self):
        # Окна нет, но is_early пришёл булевым — значит ответ настоящий,
        # и is_ended можно верить.
        self.assertEqual(
            betboom.api_info_to_status({"is_ended": True, "is_early": False}),
            "expired",
        )

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
    def setUp(self):
        # Подпись действия кэшируется по URL (см. betboom._signature_cache):
        # без сброса тест получил бы подпись, оставленную соседним тестом,
        # и страницу колеса вообще не запросил.
        betboom._signature_cache.clear()
        self.addCleanup(betboom._signature_cache.clear)

    def test_api_check_posts_normalized_freestream_url(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "code": 200,
            "status": "OK",
            "info": running_info(),
        }
        session = wheel_session(response)

        status = betboom.check_wheel_status(
            "https://betboom.ru/freestream/zonertg10?utm_source=test", session
        )

        self.assertEqual(status, "active")
        # Новый контракт: тело — action_uid со страницы, подпись — в заголовке.
        # Прежнее {"streamer_link": ...} без подписи API не отвергает, а
        # отдаёт заглушку с is_ended=true (см. fetch_wheel_info).
        self.assertEqual(
            session.post.call_args.kwargs["json"], {"action_uid": "action-uid-1"}
        )
        self.assertEqual(
            session.post.call_args.kwargs["headers"]["x-action-signature"],
            "signature-1",
        )
        # Страница берётся по канонизированному адресу, без query-параметров.
        self.assertEqual(
            session.get.call_args.args[0], "https://betboom.ru/freestream/zonertg10"
        )

    def test_api_check_returns_unknown_for_http_failure(self):
        response = Mock(status_code=503)
        session = wheel_session(response)
        self.assertEqual(
            betboom.check_wheel_status("https://betboom.ru/freestream/a", session),
            "unknown",
        )


class ActionSignatureTests(unittest.TestCase):
    """action_uid и подпись берутся со страницы колеса и переиспользуются.

    get-info принимает не адрес колеса, а его action_uid, плюс требует
    заголовок x-action-signature — иначе отвечает заглушкой (одинаковый
    ответ на любой запрос, is_ended=true), из-за которой живые колёса
    выглядели завершившимися."""

    def setUp(self):
        betboom._signature_cache.clear()
        self.addCleanup(betboom._signature_cache.clear)

    def test_signature_is_reused_within_ttl(self):
        session = wheel_session(
            Mock(status_code=200, json=Mock(return_value={"info": running_info()}))
        )
        url = "https://betboom.ru/freestream/demo"

        betboom.fetch_wheel_info(url, session)
        betboom.fetch_wheel_info(url, session)

        # Страница — один раз, API — оба: подпись живёт сутки, а статус
        # колеса меняется, и кэшировать его здесь нельзя.
        self.assertEqual(session.get.call_count, 1)
        self.assertEqual(session.post.call_count, 2)

    def test_page_without_next_data_gives_no_info(self):
        session = Mock()
        session.get.return_value = Mock(status_code=200, text="<html>пусто</html>")

        self.assertIsNone(
            betboom.fetch_wheel_info(
                "https://betboom.ru/freestream/demo", session
            )
        )
        # Без подписи запрос к API бессмысленен — он бы вернул заглушку.
        session.post.assert_not_called()

    def test_unavailable_page_gives_no_info(self):
        session = Mock()
        session.get.return_value = Mock(status_code=503, text="")

        self.assertIsNone(
            betboom.fetch_wheel_info(
                "https://betboom.ru/freestream/demo", session
            )
        )
        session.post.assert_not_called()

    def test_missing_info_falls_back_to_unknown(self):
        # Недоступная страница не должна выглядеть как «колесо завершилось»:
        # статус unknown уходит fail-open (см. модульную докстроку).
        session = Mock()
        session.get.return_value = Mock(status_code=503, text="")
        self.assertEqual(
            betboom.check_wheel_status(
                "https://betboom.ru/freestream/demo", session
            ),
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
    def setUp(self):
        # Подпись действия кэшируется по URL (см. betboom._signature_cache):
        # без сброса тест получил бы подпись, оставленную соседним тестом,
        # и страницу колеса вообще не запросил.
        betboom._signature_cache.clear()
        self.addCleanup(betboom._signature_cache.clear)

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
        session = wheel_session(response)

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
        session = wheel_session(response)

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
        betboom._signature_cache.clear()
        self.addCleanup(betboom._signature_cache.clear)

    def _expired_response(self):
        return Mock(
            status_code=200,
            json=Mock(return_value={"info": ended_info()}),
        )

    def _active_response(self):
        return Mock(
            status_code=200,
            json=Mock(return_value={"info": running_info()}),
        )

    def test_expired_status_is_served_from_cache_within_cooldown(self):
        session = wheel_session(self._expired_response())
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
        session = wheel_session(self._expired_response())
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
        session = wheel_session(self._expired_response())
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
            session = wheel_session(self._expired_response())
            session_calls.append(session)
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
            return wheel_session(self._active_response())

        after_ttl = base + timedelta(seconds=config.EXPIRED_CACHE_TTL_SECONDS + 1)
        with (
            patch("wheelsparser.betboom.build_session", fake_build_session_active),
            patch("wheelsparser.betboom.now_msk", return_value=after_ttl),
        ):
            active_items, _soon, _unknown = betboom.classify_wheels([item])
        self.assertEqual(active_items, [item])


class StubGuardTests(unittest.TestCase):
    """Аварийный выключатель на случай новой заглушки API (см.
    config.BETBOOM_STUB_GUARD_THRESHOLD и betboom._apply_stub_guard):
    подряд идущие expired без единого active/soon — при живом API
    статистически невероятная серия, поэтому после порога expired
    считается недоверенным и подменяется на unknown (fail-open).
    Срабатывание и снятие guard'а пишутся в parser.log один раз;
    сервисных уведомлений в Telegram по ним намеренно нет.
    """

    def setUp(self):
        betboom._signature_cache.clear()
        betboom._consecutive_expired = 0
        betboom._stub_guard_active = False
        self.addCleanup(betboom._signature_cache.clear)
        self.addCleanup(setattr, betboom, "_consecutive_expired", 0)
        self.addCleanup(setattr, betboom, "_stub_guard_active", False)
        patcher = patch("wheelsparser.betboom.log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def assert_guard_tripped_once(self):
        """Guard сработал: одна запись в лог, ноль уведомлений в Telegram."""
        self.log.error.assert_called_once()

    def assert_guard_silent(self):
        self.log.error.assert_not_called()

    def _expired_session(self):
        return wheel_session(
            Mock(status_code=200, json=Mock(return_value={"info": ended_info()}))
        )

    def _active_session(self):
        return wheel_session(
            Mock(status_code=200, json=Mock(return_value={"info": running_info()}))
        )

    def test_expired_passes_through_below_threshold(self):
        session = self._expired_session()
        for _ in range(config.BETBOOM_STUB_GUARD_THRESHOLD - 1):
            status = betboom.check_wheel_status(
                "https://betboom.ru/freestream/demo", session
            )
            self.assertEqual(status, "expired")
        self.assert_guard_silent()

    def test_expired_downgrades_to_unknown_at_threshold(self):
        session = self._expired_session()
        for _ in range(config.BETBOOM_STUB_GUARD_THRESHOLD - 1):
            betboom.check_wheel_status("https://betboom.ru/freestream/demo", session)
        self.assert_guard_silent()

        status = betboom.check_wheel_status(
            "https://betboom.ru/freestream/demo", session
        )

        self.assertEqual(status, "unknown")
        self.assert_guard_tripped_once()
        # Дальнейшие expired тоже уходят в unknown, но повторной записи
        # в лог быть не должно — не спамим при каждой ссылке.
        self.log.error.reset_mock()
        status = betboom.check_wheel_status(
            "https://betboom.ru/freestream/demo", session
        )
        self.assertEqual(status, "unknown")
        self.assert_guard_silent()

    def test_unknown_does_not_advance_or_reset_the_counter(self):
        # Ответ без окна и без is_early — честный unknown (см.
        # ApiStatusTests.test_rejects_incomplete_info), а не сигнал в
        # пользу или против гипотезы о заглушке: счётчик не должен ни расти,
        # ни сбрасываться из-за него.
        expired_session = self._expired_session()
        unknown_session = wheel_session(
            Mock(
                status_code=200,
                json=Mock(return_value={"info": {"is_ended": False}}),
            )
        )
        for _ in range(config.BETBOOM_STUB_GUARD_THRESHOLD - 1):
            betboom.check_wheel_status(
                "https://betboom.ru/freestream/demo", expired_session
            )
            status = betboom.check_wheel_status(
                "https://betboom.ru/freestream/demo2", unknown_session
            )
            self.assertEqual(status, "unknown")
        self.assert_guard_silent()

        status = betboom.check_wheel_status(
            "https://betboom.ru/freestream/demo", expired_session
        )
        self.assertEqual(status, "unknown")
        self.assert_guard_tripped_once()

    def test_real_active_or_soon_resets_the_counter(self):
        expired_session = self._expired_session()
        active_session = self._active_session()
        for _ in range(config.BETBOOM_STUB_GUARD_THRESHOLD - 1):
            betboom.check_wheel_status(
                "https://betboom.ru/freestream/demo", expired_session
            )

        status = betboom.check_wheel_status(
            "https://betboom.ru/freestream/other", active_session
        )
        self.assertEqual(status, "active")

        # Счётчик сброшен: тот же почти-порог expired снова проходит без
        # подмены на unknown.
        self.log.error.reset_mock()
        for _ in range(config.BETBOOM_STUB_GUARD_THRESHOLD - 1):
            status = betboom.check_wheel_status(
                "https://betboom.ru/freestream/demo", expired_session
            )
            self.assertEqual(status, "expired")
        self.assert_guard_silent()

    def test_recovery_after_trip_logs_once_and_reopens_guard(self):
        expired_session = self._expired_session()
        active_session = self._active_session()
        for _ in range(config.BETBOOM_STUB_GUARD_THRESHOLD):
            betboom.check_wheel_status(
                "https://betboom.ru/freestream/demo", expired_session
            )
        self.assert_guard_tripped_once()

        self.log.error.reset_mock()
        self.log.info.reset_mock()
        status = betboom.check_wheel_status(
            "https://betboom.ru/freestream/other", active_session
        )
        self.assertEqual(status, "active")
        self.log.info.assert_called_once()  # снятие подозрения — одна запись в лог

        # Гипотеза может подтвердиться снова: новая серия expired обязана
        # заново сработать (guard не остаётся навсегда "уже отметил").
        self.log.error.reset_mock()
        for _ in range(config.BETBOOM_STUB_GUARD_THRESHOLD):
            betboom.check_wheel_status(
                "https://betboom.ru/freestream/demo", expired_session
            )
        self.assert_guard_tripped_once()

    def test_downgraded_expired_is_not_written_to_expired_cache(self):
        # precheck_wheel кэширует expired на EXPIRED_CACHE_TTL_SECONDS —
        # если guard подменил статус на unknown, кэшировать как expired
        # нельзя: иначе настоящий active потом ждал бы TTL кэша вместо
        # немедленного обнаружения. Разные URL — реальный сбой заглушки
        # выглядит как поток РАЗНЫХ колёс подряд, а не повтор одного и
        # того же адреса (тот уже гасится _expired_cache, см. тест ниже).
        betboom._expired_cache.clear()
        self.addCleanup(betboom._expired_cache.clear)
        session = self._expired_session()
        last_url = ""
        for i in range(config.BETBOOM_STUB_GUARD_THRESHOLD):
            last_url = f"https://betboom.ru/freestream/demo{i}"
            betboom.precheck_wheel(last_url, session)

        self.assertFalse(betboom._is_cached_expired(last_url))

    def test_expired_cache_hit_does_not_feed_the_counter(self):
        # Один и тот же URL, повторно найденный в нескольких постах подряд,
        # не должен приближать порог: после первого свежего expired
        # _expired_cache гасит все повторы без обращения к API, и они не
        # являются новым свидетельством в пользу заглушки.
        betboom._expired_cache.clear()
        self.addCleanup(betboom._expired_cache.clear)
        session = self._expired_session()
        for _ in range(config.BETBOOM_STUB_GUARD_THRESHOLD * 2):
            status, *_ = betboom.precheck_wheel(
                "https://betboom.ru/freestream/demo", session
            )
            self.assertEqual(status, "expired")
        self.assert_guard_silent()


class RegressionTests(unittest.TestCase):
    def test_active_check_does_not_require_playwright(self):
        self.assertFalse(hasattr(betboom, "async_playwright"))

    def test_active_max_age_is_twenty_hours_by_default(self):
        self.assertEqual(config.ACTIVE_MAX_AGE_HOURS, 20)


if __name__ == "__main__":
    unittest.main()
