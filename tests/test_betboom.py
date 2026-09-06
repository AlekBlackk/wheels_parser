import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from wheelsparser import alerts, betboom, config
from wheelsparser.timeutils import now_msk


def running_info(**extra):
    """info запущенного колеса: старт минуту назад, розыгрыш идёт полчаса.

    action_uid обязателен: по его наличию настоящий ответ отличается от
    заглушки (см. stub_info и betboom.api_info_to_status).
    """
    started = datetime.now(timezone.utc) - timedelta(minutes=1)
    return {
        "action_uid": "action-uid-1",
        "is_ended": False,
        "is_early": False,
        "start_dttm": started.isoformat().replace("+00:00", "Z"),
        "duration_min": 30,
        **extra,
    }


def ended_info(**extra):
    """info завершённого колеса: полный ответ API, а не заглушка.

    is_ended в одиночку статусом не считается (см. stub_info), поэтому
    тестам про expired нужен ответ с action_uid и окном розыгрыша.
    """
    started = datetime.now(timezone.utc) - timedelta(hours=2)
    return {
        "action_uid": "action-uid-1",
        "is_ended": True,
        "is_early": False,
        "start_dttm": started.isoformat().replace("+00:00", "Z"),
        "duration_min": 30,
        **extra,
    }


def stub_info(**extra):
    """Ответ-заглушка API BetBoom: is_ended=true и ни одного пригодного поля.

    Опознаётся по отсутствию action_uid/action_id — в настоящем ответе
    идентификатор действия есть всегда. Такую заглушку API отдаёт на
    неверный контракт запроса: без заголовка x-action-signature, с чужой
    подписью или в старом виде {streamer_link}. start_dttm в ней — время
    запроса, а не старт розыгрыша; duration_min и is_early отсутствуют.
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
        # is_ended=true в заглушке приходит и для живого колеса, поэтому
        # верить ему нельзя. Признак заглушки — отсутствие action_uid.
        self.assertEqual(betboom.api_info_to_status(stub_info()), "unknown")

    def test_treats_response_without_action_id_as_stub(self):
        # Даже правдоподобное окно розыгрыша не делает ответ настоящим:
        # без идентификатора действия это не info колеса.
        self.assertEqual(
            betboom.api_info_to_status({
                "is_ended": True,
                "is_early": False,
                "start_dttm": "2026-08-30T10:00:00Z",
                "duration_min": 30,
            }),
            "unknown",
        )

    def test_accepts_response_identified_by_action_id_alone(self):
        # У части ответов приходит только числовой action_id — этого
        # достаточно, чтобы считать ответ настоящим.
        self.assertEqual(
            betboom.api_info_to_status({
                "action_id": 2010,
                "is_ended": True,
                "is_early": False,
            }),
            "expired",
        )

    def test_expires_ended_wheel_with_known_window(self):
        # Настоящий ответ с окном розыгрыша: is_ended=true означает
        # «завершилось».
        self.assertEqual(
            betboom.api_info_to_status({
                "action_uid": "action-uid-1",
                "is_ended": True,
                "is_early": False,
                "start_dttm": "2026-08-30T10:00:00Z",
                "duration_min": 30,
            }),
            "expired",
        )

    def test_expires_ended_wheel_with_is_early_flag(self):
        # Окна нет, но ответ настоящий (есть action_uid) — is_ended можно
        # верить.
        self.assertEqual(
            betboom.api_info_to_status(
                {"action_uid": "a", "is_ended": True, "is_early": False}
            ),
            "expired",
        )

    def test_marks_early_wheel_soon(self):
        self.assertEqual(
            betboom.api_info_to_status(
                {"action_uid": "a", "is_ended": False, "is_early": True}
            ),
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
            betboom.api_info_to_status({
                "action_uid": "a",
                "is_ended": False,
                "is_early": False,
                "duration_min": 30,
            }),
            "soon",
        )

    def test_marks_wheel_with_future_start_soon(self):
        start = datetime.now(timezone.utc) + timedelta(minutes=5)
        self.assertEqual(
            betboom.api_info_to_status({
                "action_uid": "action-uid-1",
                "is_ended": False,
                "is_early": False,
                "start_dttm": start.isoformat().replace("+00:00", "Z"),
                "duration_min": 30,
            }),
            "soon",
        )

    def test_rejects_identified_response_without_any_window(self):
        # Идентификатор действия есть, но о состоянии колеса ответ молчит:
        # ни окна розыгрыша, ни булева is_early. Это неудача проверки, а
        # не «розыгрыш ещё не начался» — иначе /active перестанет считать
        # такие колёса непроверенными, а счётчик здоровья API сбросится.
        self.assertEqual(
            betboom.api_info_to_status(
                {"action_uid": "a", "is_ended": False, "title": "КОЛЕСО"}
            ),
            "unknown",
        )

    def test_rejects_incomplete_info(self):
        # Ни идентификатора действия, ни окна — ответ не сообщает о
        # колесе ничего.
        self.assertEqual(betboom.api_info_to_status({"is_ended": False}), "unknown")

    def test_expires_wheel_whose_duration_has_passed(self):
        self.assertEqual(
            betboom.api_info_to_status({
                "action_uid": "action-uid-1",
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

        status, _, _ = betboom.precheck_wheel(
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
            betboom.precheck_wheel("https://betboom.ru/freestream/a", session)[0],
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
            betboom.precheck_wheel(
                "https://betboom.ru/freestream/demo", session
            )[0],
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


class StatusHealthTests(unittest.TestCase):
    """Детектор «проверка статуса ослепла» (см. betboom._note_status_health).

    Раньше здесь был fail-open: после порога подряд идущих expired все
    следующие expired подменялись на unknown и уходили в Telegram. Серия
    expired подряд — штатное состояние (колёса живут часами, а находятся
    сутками), поэтому гвард исправно рассылал завершившиеся колёса. Теперь
    он ничего не подменяет: считает подряд идущие unknown и один раз пишет
    в parser.log, что проверка перестала давать результат. Сервисных
    уведомлений в Telegram по нему по-прежнему нет.
    """

    def setUp(self):
        betboom._signature_cache.clear()
        betboom._consecutive_unknown = 0
        betboom._blind_warning_logged = False
        betboom._consecutive_without_live = 0
        betboom._silence_warning_logged = False
        self.addCleanup(betboom._signature_cache.clear)
        self.addCleanup(setattr, betboom, "_consecutive_unknown", 0)
        self.addCleanup(setattr, betboom, "_blind_warning_logged", False)
        self.addCleanup(setattr, betboom, "_consecutive_without_live", 0)
        self.addCleanup(setattr, betboom, "_silence_warning_logged", False)
        patcher = patch("wheelsparser.betboom.log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)

    def assert_warned_once(self):
        """Предупреждение ушло: одна запись в лог, ноль сообщений в Telegram."""
        self.log.error.assert_called_once()

    def assert_silent(self):
        self.log.error.assert_not_called()

    def _expired_session(self):
        return wheel_session(
            Mock(status_code=200, json=Mock(return_value={"info": ended_info()}))
        )

    def _active_session(self):
        return wheel_session(
            Mock(status_code=200, json=Mock(return_value={"info": running_info()}))
        )

    def _stub_session(self):
        return wheel_session(
            Mock(status_code=200, json=Mock(return_value={"info": stub_info()}))
        )

    def _status(self, url: str, session: object) -> str:
        return betboom.precheck_wheel(url, session, use_cache=False)[0]  # type: ignore[arg-type]

    def test_expired_series_never_warns(self):
        # Главный регресс: серия expired — норма работы парсера, а не сбой.
        session = self._expired_session()
        for i in range(config.BETBOOM_STUB_GUARD_THRESHOLD * 2):
            status = self._status(f"https://betboom.ru/freestream/demo{i}", session)
            self.assertEqual(status, "expired")
        self.assert_silent()

    def test_unknown_series_warns_once_at_threshold(self):
        session = self._stub_session()
        for i in range(config.BETBOOM_STUB_GUARD_THRESHOLD - 1):
            self.assertEqual(
                self._status(f"https://betboom.ru/freestream/demo{i}", session),
                "unknown",
            )
        self.assert_silent()

        status = self._status("https://betboom.ru/freestream/last", session)

        self.assertEqual(status, "unknown")
        self.assert_warned_once()
        # Статус не подменяется и после срабатывания, а в лог не спамим
        # при каждой следующей ссылке.
        self.log.error.reset_mock()
        self.assertEqual(
            self._status("https://betboom.ru/freestream/more", session), "unknown"
        )
        self.assert_silent()

    def test_any_determined_status_resets_the_counter(self):
        stub_session = self._stub_session()
        active_session = self._active_session()
        for i in range(config.BETBOOM_STUB_GUARD_THRESHOLD - 1):
            self._status(f"https://betboom.ru/freestream/demo{i}", stub_session)

        self.assertEqual(
            self._status("https://betboom.ru/freestream/live", active_session),
            "active",
        )

        for i in range(config.BETBOOM_STUB_GUARD_THRESHOLD - 1):
            self._status(f"https://betboom.ru/freestream/again{i}", stub_session)
        self.assert_silent()

    def test_recovery_is_logged_once_and_rearms_the_warning(self):
        stub_session = self._stub_session()
        active_session = self._active_session()
        for i in range(config.BETBOOM_STUB_GUARD_THRESHOLD):
            self._status(f"https://betboom.ru/freestream/demo{i}", stub_session)
        self.assert_warned_once()

        self.log.error.reset_mock()
        self.log.info.reset_mock()
        self._status("https://betboom.ru/freestream/live", active_session)
        self.assertTrue(
            any(
                "статус снова определяется" in str(call)
                for call in self.log.info.call_args_list
            )
        )

        # Поломка может повториться: новая серия unknown обязана снова
        # предупредить, а не остаться «уже отмеченной» навсегда.
        self.log.error.reset_mock()
        for i in range(config.BETBOOM_STUB_GUARD_THRESHOLD):
            self._status(f"https://betboom.ru/freestream/back{i}", stub_session)
        self.assert_warned_once()

    def test_expired_cache_hit_does_not_feed_the_counter(self):
        # Повторы одного URL из нескольких постов гасит _expired_cache без
        # обращения к API — новым свидетельством о здоровье API они не
        # являются и счётчик двигать не должны.
        betboom._expired_cache.clear()
        self.addCleanup(betboom._expired_cache.clear)
        session = self._expired_session()
        for _ in range(config.BETBOOM_STUB_GUARD_THRESHOLD * 2):
            status, *_ = betboom.precheck_wheel(
                "https://betboom.ru/freestream/demo", session
            )
            self.assertEqual(status, "expired")
        self.assert_silent()

    def test_long_series_without_live_wheel_warns_once(self):
        # Заглушку, которая отдаёт правдоподобный expired на каждый адрес,
        # по форме ответа не отличить: снаружи она выглядит как парсер,
        # который исправно работает и молчит. Единственный признак —
        # что живого колеса не видно очень долго.
        session = self._expired_session()
        for i in range(betboom.NO_LIVE_WHEEL_THRESHOLD - 1):
            self._status(f"https://betboom.ru/freestream/demo{i}", session)
        self.assert_silent()

        self._status("https://betboom.ru/freestream/last", session)

        self.assert_warned_once()
        self.log.error.reset_mock()
        self._status("https://betboom.ru/freestream/more", session)
        self.assert_silent()

    def test_live_wheel_clears_the_silence_suspicion(self):
        expired_session = self._expired_session()
        active_session = self._active_session()
        for i in range(betboom.NO_LIVE_WHEEL_THRESHOLD):
            self._status(f"https://betboom.ru/freestream/demo{i}", expired_session)
        self.assert_warned_once()

        self.log.info.reset_mock()
        self._status("https://betboom.ru/freestream/live", active_session)

        self.assertTrue(
            any(
                "подозрение на заглушку снято" in str(call)
                for call in self.log.info.call_args_list
            )
        )
        self.assertEqual(betboom._consecutive_without_live, 0)

    def test_missing_page_does_not_count_as_a_live_wheel(self):
        # 404 ничего не говорит о том, способен ли API показать живое
        # колесо, поэтому подозрение он снимать не должен.
        missing_session = Mock()
        missing_session.get.return_value = Mock(status_code=404, text="")

        self._status("https://betboom.ru/freestream/nosuchslug", missing_session)

        self.assertEqual(betboom._consecutive_without_live, 1)

    def test_classify_wheels_does_not_feed_the_counter(self):
        # /active обходит все колёса за сутки: его сбои не должны говорить
        # за рабочие потоки (parser, twitch).
        betboom._expired_cache.clear()
        self.addCleanup(betboom._expired_cache.clear)
        items = [
            {"url": f"https://betboom.ru/freestream/stub{i}"}
            for i in range(config.BETBOOM_STUB_GUARD_THRESHOLD + 3)
        ]
        with patch(
            "wheelsparser.betboom.build_session", side_effect=self._stub_session
        ):
            active, soon, unknown = betboom.classify_wheels(items)

        self.assertEqual(active, [])
        self.assertEqual(soon, [])
        self.assertEqual(unknown, len(items))
        self.assertEqual(betboom._consecutive_unknown, 0)
        self.assertFalse(betboom._blind_warning_logged)
        self.assert_silent()


class ClassifyMissingWheelsTests(unittest.TestCase):
    """Колесо с исчезнувшей страницей не попадает ни в одну корзину /active."""

    def setUp(self):
        betboom._signature_cache.clear()
        betboom._expired_cache.clear()
        self.addCleanup(betboom._signature_cache.clear)
        self.addCleanup(betboom._expired_cache.clear)

    def test_missing_wheel_is_neither_active_soon_nor_unknown(self):
        # unknown_count — это «проверить не удалось», а про удалённое
        # колесо всё известно: показывать его в /active незачем и
        # пугать им человека в строке «не удалось проверить» — тоже.
        def missing_session():
            session = Mock()
            session.get.return_value = Mock(status_code=404, text="")
            return session

        items = [{"url": "https://betboom.ru/freestream/gone"}]
        with patch(
            "wheelsparser.betboom.build_session", side_effect=missing_session
        ):
            active, soon, unknown = betboom.classify_wheels(items)

        self.assertEqual(active, [])
        self.assertEqual(soon, [])
        self.assertEqual(unknown, 0)


class MissingWheelPageTests(unittest.TestCase):
    """HTTP 404 на странице колеса — отдельный статус 'missing'.

    Такой адрес не существует (опечатка, обрезанная ссылка, удалённое
    колесо), и от 'unknown' он отличается тем, что перепроверять его
    бессмысленно: колесо там не появится.
    """

    def setUp(self):
        betboom._signature_cache.clear()
        self.addCleanup(betboom._signature_cache.clear)

    def _missing_session(self):
        session = Mock()
        session.get.return_value = Mock(status_code=404, text="")
        return session

    def test_precheck_reports_missing_for_404_page(self):
        status, _referral, ends_at = betboom.precheck_wheel(
            "https://betboom.ru/freestream/nosuchslug", self._missing_session()
        )

        self.assertEqual(status, "missing")
        self.assertEqual(ends_at, "")

    def test_other_page_errors_stay_unknown(self):
        # 500 или таймаут — временный сбой, а не отсутствие колеса.
        session = Mock()
        session.get.return_value = Mock(status_code=500, text="")

        status, *_ = betboom.precheck_wheel(
            "https://betboom.ru/freestream/slug", session
        )

        self.assertEqual(status, "unknown")

    def test_missing_page_does_not_reach_the_api(self):
        session = self._missing_session()

        betboom.precheck_wheel("https://betboom.ru/freestream/nosuchslug", session)

        session.post.assert_not_called()


class CandidateGateTests(unittest.TestCase):
    """Уведомление уходит только по явному 'active' (белый список).

    Прежний чёрный список ("expired", "soon") пропускал в Telegram всё
    остальное, включая 'unknown' — а тот возникает при любом сбое сети,
    протухшей подписи и несуществующем адресе. Это и давало поток
    уведомлений о неактивных колёсах.
    """

    def setUp(self):
        alerts.LAST_URL_ALERT.clear()
        self.addCleanup(alerts.LAST_URL_ALERT.clear)

    def _run(self, status: str):
        url = f"https://betboom.ru/freestream/{status}slug"
        return betboom.process_candidate_wheel(
            url,
            "channel",
            now_msk(),
            precheck_fn=lambda *args, **kwargs: (status, False, ""),
        )

    def test_active_wheel_is_notified(self):
        entry, status, retry_needed = self._run("active")

        self.assertIsNotNone(entry)
        self.assertEqual(status, "active")
        self.assertFalse(retry_needed)

    def test_unknown_wheel_is_not_notified_but_stays_for_retry(self):
        entry, status, retry_needed = self._run("unknown")

        self.assertIsNone(entry)
        self.assertEqual(status, "unknown")
        self.assertTrue(retry_needed)

    def test_missing_wheel_is_not_notified_but_stays_for_retry(self):
        # 404 бывает не только у выдуманного слага, но и у живого
        # колеса при блокировке или сбое CDN — терять такую ссылку
        # навсегда дороже, чем сходить по ней ещё несколько раз.
        entry, status, retry_needed = self._run("missing")

        self.assertIsNone(entry)
        self.assertEqual(status, "missing")
        self.assertTrue(retry_needed)

    def test_expired_and_soon_stay_silent_and_retryable(self):
        for status in ("expired", "soon", "unknown", "missing"):
            with self.subTest(status=status):
                entry, reported, retry_needed = self._run(status)
                self.assertIsNone(entry)
                self.assertEqual(reported, status)
                self.assertTrue(retry_needed)

    def test_skipped_wheel_releases_the_cooldown_claim(self):
        # Иначе перезапуск колеса на том же адресе молчал бы весь кулдаун.
        url = "https://betboom.ru/freestream/unknownslug"
        self._run("unknown")

        self.assertNotIn(url, alerts.LAST_URL_ALERT)

    def test_disabled_precheck_still_notifies(self):
        # PRECHECK_WHEELS=false — осознанный отказ от проверки, а не её
        # неудача: пустой статус проходит белый список.
        with patch.object(betboom, "PRECHECK_WHEELS", False):
            entry, status, retry_needed = betboom.process_candidate_wheel(
                "https://betboom.ru/freestream/anyslug",
                "channel",
                now_msk(),
            )

        self.assertIsNotNone(entry)
        self.assertEqual(status, "")
        self.assertFalse(retry_needed)


class RegressionTests(unittest.TestCase):
    def test_active_check_does_not_require_playwright(self):
        self.assertFalse(hasattr(betboom, "async_playwright"))

    def test_active_max_age_is_twenty_hours_by_default(self):
        self.assertEqual(config.ACTIVE_MAX_AGE_HOURS, 20)


if __name__ == "__main__":
    unittest.main()
