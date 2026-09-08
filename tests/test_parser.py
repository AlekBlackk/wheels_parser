import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import datetime as dt
from datetime import timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from bs4 import BeautifulSoup

from tests.dbfixture import entries_since, use_temp_db
from wheelsparser import (
    alerts,
    betboom,
    config,
    db,
    menu,
    parser,
    registry,
    storage,
    twitch,
    urls,
)


def make_message(message_id, text, links, disallowed_domains=()):
    """Сообщение в том виде, в каком его отдаёт fetch_channel."""
    return {
        "id": message_id,
        "text": text,
        "preview_html": text,
        "urls": links,
        "disallowed_domains": list(disallowed_domains),
        "hash": urls.message_content_hash(text, links),
        "legacy_hash": urls.message_content_hash(text, links),
        "message_url": f"https://t.me/{message_id}",
    }


class FetchChannelTests(unittest.TestCase):
    def test_extracts_message_data(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        # Именно content: fetch_channel читает байты, чтобы BeautifulSoup
        # сам определил кодировку страницы.
        response.content = """
        <div class="tgme_widget_message_wrap">
          <div class="tgme_widget_message" data-post="demo/42">
            <div class="tgme_widget_message_text">
              Новое колесо: <a href="https://betboom.ru/freestream/abc">ссылка</a>
            </div>
          </div>
        </div>
        """.encode()

        with patch.object(parser.PARSER_SESSION, "get", return_value=response) as get:
            messages = parser.fetch_channel("demo")

        get.assert_called_once_with(
            "https://t.me/s/demo", timeout=config.CHANNEL_FETCH_TIMEOUT
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["id"], "demo/42")
        self.assertEqual(messages[0]["message_url"], "https://t.me/demo/42")
        self.assertEqual(messages[0]["urls"], ["https://betboom.ru/freestream/abc"])
        self.assertIn("Новое колесо", messages[0]["text"])

    def test_returns_none_for_not_found(self):
        response = Mock(status_code=404)
        with patch.object(parser.PARSER_SESSION, "get", return_value=response):
            self.assertIsNone(parser.fetch_channel("missing"))

    def test_returns_none_for_http_error(self):
        response = Mock(status_code=500)
        response.raise_for_status.side_effect = requests.HTTPError("server error")
        with patch.object(parser.PARSER_SESSION, "get", return_value=response):
            self.assertIsNone(parser.fetch_channel("broken"))

    def test_returns_none_for_network_error(self):
        with patch.object(
            parser.PARSER_SESSION,
            "get",
            side_effect=requests.Timeout("connection timed out"),
        ):
            self.assertIsNone(parser.fetch_channel("offline"))

    def test_shortlink_in_post_is_resolved_via_the_channel_session(self):
        # Раскрытие сокращателя должно идти через ту же сессию, что и обход
        # канала (см. fetch_channel: http_session = session or PARSER_SESSION).
        urls._shortlink_cache.clear()
        self.addCleanup(urls._shortlink_cache.clear)
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.content = """
        <div class="tgme_widget_message_wrap">
          <div class="tgme_widget_message" data-post="demo/42">
            <div class="tgme_widget_message_text">
              Колесо <a href="https://vk.cc/aB3xZ">тут</a>
            </div>
          </div>
        </div>
        """.encode()
        redirect = Mock(
            status_code=301,
            headers={"Location": "https://betboom.ru/freestream/hidden"},
        )

        with patch.object(parser.PARSER_SESSION, "get", return_value=response), \
             patch.object(parser.PARSER_SESSION, "head", return_value=redirect) as head:
            messages = parser.fetch_channel("demo")

        self.assertEqual(
            messages[0]["urls"], ["https://betboom.ru/freestream/hidden"]
        )
        head.assert_called_once()

    def test_extracts_forward_source_from_repost(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.content = """
        <div class="tgme_widget_message_wrap">
          <div class="tgme_widget_message" data-post="demo/42">
            <a class="tgme_widget_message_forwarded_from_name"
               href="https://t.me/origchannel">Первоисточник</a>
            <div class="tgme_widget_message_text">
              Колесо <a href="https://betboom.ru/freestream/abc">тут</a>
            </div>
          </div>
        </div>
        """.encode()

        with patch.object(parser.PARSER_SESSION, "get", return_value=response):
            messages = parser.fetch_channel("demo")

        self.assertEqual(messages[0]["forwarded_from"], "origchannel")

    def test_returns_none_instead_of_raising_on_parse_crash(self):
        # Ошибка разбора одного канала (не requests.RequestException) не
        # должна вылетать наружу: у fetch_channel есть свой try/except,
        # а в ThreadPoolExecutor (_fetch_all_channels) необработанное
        # исключение здесь оборвало бы весь цикл по всем каналам разом.
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.content = """
        <div class="tgme_widget_message_wrap">
          <div class="tgme_widget_message" data-post="demo/1">
            <div class="tgme_widget_message_text">колесо</div>
          </div>
        </div>
        """.encode()

        with patch.object(parser.PARSER_SESSION, "get", return_value=response), \
             patch.object(
                 parser, "message_preview_html", side_effect=RecursionError("boom")
             ):
            self.assertIsNone(parser.fetch_channel("broken-markup"))


class MessagePreviewHtmlTests(unittest.TestCase):
    def test_betboom_link_preserves_anchor_tag(self):
        html = BeautifulSoup(
            '<div class="tgme_widget_message_text">'
            'Колесо: <a href="https://betboom.ru/freestream/demo">клик</a>'
            '</div>',
            "html.parser",
        ).find("div")
        preview = parser.message_preview_html(html)
        self.assertIn('<a href="https://betboom.ru/freestream/demo">клик</a>', preview)

    def test_third_party_link_rendered_as_plain_text(self):
        html = BeautifulSoup(
            '<div class="tgme_widget_message_text">'
            'Наш чат: <a href="https://t.me/channel">канал</a>'
            '</div>',
            "html.parser",
        ).find("div")
        preview = parser.message_preview_html(html)
        self.assertNotIn("<a ", preview)
        self.assertIn("канал (https://t.me/channel)", preview)

    def test_spoofed_link_unmasked_as_text(self):
        html = BeautifulSoup(
            '<div class="tgme_widget_message_text">'
            '<a href="https://t.me/fake_bot">https://betboom.ru/freestream/wheel1</a>'
            '</div>',
            "html.parser",
        ).find("div")
        preview = parser.message_preview_html(html)
        self.assertNotIn("<a ", preview)
        self.assertIn("https://betboom.ru/freestream/wheel1 (https://t.me/fake_bot)", preview)

    def test_identical_label_and_href_rendered_without_duplication(self):
        html = BeautifulSoup(
            '<div class="tgme_widget_message_text">'
            '<a href="https://t.me/channel">https://t.me/channel</a>'
            '</div>',
            "html.parser",
        ).find("div")
        preview = parser.message_preview_html(html)
        self.assertNotIn("<a ", preview)
        self.assertEqual(preview, "https://t.me/channel")


def forward_html(href=None, tag="a"):
    """Пост-репост в разметке веб-превью t.me/s."""
    attribute = f' href="{href}"' if href is not None else ""
    return BeautifulSoup(
        f'<div class="tgme_widget_message">'
        f'<{tag} class="tgme_widget_message_forwarded_from_name"{attribute}>'
        f"Первоисточник</{tag}>"
        f'<div class="tgme_widget_message_text">колесо</div></div>',
        "html.parser",
    )


class ForwardedFromChannelTests(unittest.TestCase):
    """Первоисточник репоста — кандидат в мониторинг (см.
    parser.suggest_forward_source). href из чужой разметки доверенным
    вводом не является и проходит тот же USERNAME_RE, что и /add."""

    def test_extracts_channel_from_forward_header(self):
        self.assertEqual(
            parser.forwarded_from_channel(forward_html("https://t.me/origchannel")),
            "origchannel",
        )

    def test_extracts_channel_when_href_points_at_exact_post(self):
        self.assertEqual(
            parser.forwarded_from_channel(forward_html("https://t.me/origchannel/1234")),
            "origchannel",
        )

    def test_post_without_forward_header_has_no_source(self):
        html = BeautifulSoup(
            '<div class="tgme_widget_message">'
            '<div class="tgme_widget_message_text">колесо</div></div>',
            "html.parser",
        )
        self.assertEqual(parser.forwarded_from_channel(html), "")

    def test_hidden_source_without_href_is_ignored(self):
        # Репост из закрытого канала: тот же класс, но <span> без href —
        # первоисточник Telegram не раскрывает, добавлять нечего.
        self.assertEqual(parser.forwarded_from_channel(forward_html(tag="span")), "")

    def test_foreign_host_is_ignored(self):
        self.assertEqual(
            parser.forwarded_from_channel(forward_html("https://evil.example/channel")),
            "",
        )

    def test_private_invite_link_is_ignored(self):
        # t.me/+hash — приглашение в закрытый канал, а не юзернейм:
        # такой «канал» парсер читать не сможет.
        self.assertEqual(
            parser.forwarded_from_channel(forward_html("https://t.me/+AbCdEfGh")), ""
        )

    def test_preview_path_is_ignored(self):
        self.assertEqual(
            parser.forwarded_from_channel(forward_html("https://t.me/s/origchannel")),
            "",
        )


class SuggestForwardSourceTests(unittest.TestCase):
    """Репост колеса выдаёт канал-первоисточник — самый дешёвый способ
    найти того, кто постит колёса раньше отслеживаемых каналов."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wheelsparser-suggest-"))
        self._patch(patch.object(
            storage, "SUGGESTED_CHANNELS_FILE", self.tmp / "suggested_channels.json"
        ))
        self._patch(patch.object(storage, "SUGGESTED_CHANNELS", set()))
        self._patch(patch.object(registry, "CHANNELS", ["watched"]))
        # registry.KEYWORDS наполняется registry.init() при старте приложения,
        # в тестах он пуст — задаём явно, как остальные тесты про слова.
        self._patch(patch.object(registry, "KEYWORDS", ["колесо"]))
        self.notify = self._patch(patch.object(parser, "send_service_notification"))

    def _patch(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def _message(self, forwarded_from="origchannel", urls=None, text="колесо"):
        message = make_message("watched/1", text, urls or [])
        message["forwarded_from"] = forwarded_from
        return message

    def test_suggests_unmonitored_source_of_a_wheel_repost(self):
        parser.suggest_forward_source(
            self._message(urls=["https://betboom.ru/freestream/a"]), "watched"
        )

        self.notify.assert_called_once()
        text = self.notify.call_args.args[0]
        self.assertIn("@origchannel", text)
        self.assertEqual(
            self.notify.call_args.kwargs["reply_markup"],
            menu.channel_suggestion_keyboard("origchannel"),
        )

    def test_suggests_source_of_a_keyword_only_repost(self):
        parser.suggest_forward_source(self._message(text="розыгрыш колесо"), "watched")

        self.notify.assert_called_once()

    def test_post_without_wheel_or_keyword_is_not_suggested(self):
        parser.suggest_forward_source(self._message(text="всем привет"), "watched")

        self.notify.assert_not_called()

    def test_post_without_forward_header_is_not_suggested(self):
        parser.suggest_forward_source(
            self._message(forwarded_from="", urls=["https://betboom.ru/freestream/a"]),
            "watched",
        )

        self.notify.assert_not_called()

    def test_already_monitored_source_is_not_suggested(self):
        # Регистр юзернеймов Telegram не важен: @Watched и @watched — один
        # канал, и предлагать добавить уже отслеживаемый нельзя.
        parser.suggest_forward_source(
            self._message(
                forwarded_from="WATCHED", urls=["https://betboom.ru/freestream/a"]
            ),
            "other",
        )

        self.notify.assert_not_called()

    def test_repost_inside_the_same_channel_is_not_suggested(self):
        parser.suggest_forward_source(
            self._message(
                forwarded_from="watched", urls=["https://betboom.ru/freestream/a"]
            ),
            "watched",
        )

        self.notify.assert_not_called()

    def test_same_source_is_suggested_only_once(self):
        message = self._message(urls=["https://betboom.ru/freestream/a"])

        parser.suggest_forward_source(message, "watched")
        parser.suggest_forward_source(message, "watched")

        self.notify.assert_called_once()

    def test_suggestion_survives_restart(self):
        # Молчание админа — тоже ответ: после рестарта то же предложение
        # приходить заново не должно (см. storage.mark_channel_suggested).
        message = self._message(urls=["https://betboom.ru/freestream/a"])
        parser.suggest_forward_source(message, "watched")
        self.notify.reset_mock()

        # Имитируем рестарт: состояние в памяти сброшено, файл остался.
        with patch.object(storage, "SUGGESTED_CHANNELS", None):
            parser.suggest_forward_source(message, "watched")

        self.notify.assert_not_called()

    def test_suppressed_wheel_still_reveals_the_source(self):
        # Самый частый случай: репост несёт ссылку, о которой уже
        # оповестили из другого канала (кулдаун гасит уведомление) —
        # находки нет, а первоисточник есть. Предложение обязано уйти.
        # Текст без ключевого слова — иначе сработал бы штатный фолбэк
        # «ссылки отсеяны, шлём по ключевому слову», и сценарий перестал
        # бы быть чистым «уведомления нет, а первоисточник есть».
        url = "https://betboom.ru/freestream/a"
        message = make_message("watched/1", "смотрите тут", [url])
        message["forwarded_from"] = "origchannel"

        with patch.dict(alerts.LAST_URL_ALERT, {url: parser.now_msk()}, clear=True), \
             patch.object(parser, "precheck_wheel", return_value=("active", False, "")), \
             patch.object(parser, "send_telegram_notification") as send:
            entries = parser.process_message(
                message, "watched", {}, False, parser.now_msk(), {url: parser.now_msk()}
            )

        self.assertEqual(entries, [])
        send.assert_not_called()
        self.notify.assert_called_once()


class ProcessMessageTests(unittest.TestCase):
    """process_message без сети: precheck и отправка замоканы."""

    def _start(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def setUp(self):
        # Кулдаун и реестр ретрая expired-ссылок глобальны для процесса —
        # изолируем тесты друг от друга.
        self._start(patch.dict(alerts.LAST_URL_ALERT, clear=True))
        self._start(patch.dict(parser.PENDING_EXPIRED_RETRY, clear=True))
        self._start(patch.object(parser, "precheck_wheel", return_value=("active", False, "")))
        self.single = self._start(
            patch.object(parser, "send_telegram_notification", return_value=True)
        )
        self.multi = self._start(
            patch.object(parser, "send_multi_telegram_notification", return_value=True)
        )
        self.now = parser.now_msk()

    def process(self, message, channel_seen, baseline=False):
        return parser.process_message(
            message, "demo", channel_seen, baseline, self.now, {}
        )

    def test_new_message_with_link_is_notified_once(self):
        message = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/a"])
        channel_seen = {}

        first = self.process(message, channel_seen)
        second = self.process(message, channel_seen)

        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.single.assert_called_once()

    def test_baseline_cycle_records_hash_without_notifying(self):
        message = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/a"])
        channel_seen = {}

        self.assertEqual(self.process(message, channel_seen, baseline=True), [])
        self.single.assert_not_called()
        self.assertEqual(channel_seen["demo/1"], message["hash"])

    def test_edited_message_with_new_link_is_notified_as_edit(self):
        original = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/a"])
        edited = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/b"])
        channel_seen = {}

        self.process(original, channel_seen)
        entries = self.process(edited, channel_seen)

        self.assertEqual(len(entries), 1)
        self.assertTrue(entries[0]["edited"])

    def test_legacy_hash_match_is_not_treated_as_edit(self):
        message = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/a"])
        # Состояние, записанное старой версией парсера: хэш из legacy-формата.
        channel_seen = {"demo/1": message["legacy_hash"]}
        message["hash"] = "0" * 16

        self.assertEqual(self.process(message, channel_seen), [])
        self.single.assert_not_called()

    def test_two_links_in_one_post_produce_single_multi_notification(self):
        message = make_message(
            "demo/1",
            "колесо",
            [
                "https://betboom.ru/freestream/a",
                "https://betboom.ru/freestream/b",
            ],
        )

        entries = self.process(message, {})

        self.assertEqual(len(entries), 2)
        self.multi.assert_called_once()
        self.single.assert_not_called()

    def test_post_text_is_used_as_referral_signal_for_single_link(self):
        message = make_message(
            "demo/1", "Колесо для рефов 🔥", ["https://betboom.ru/freestream/a"]
        )
        with patch.object(
            parser, "precheck_wheel", return_value=("active", True, "")
        ) as precheck:
            entries = self.process(message, {})

        self.assertEqual(precheck.call_args.kwargs["post_text"], "Колесо для рефов 🔥")
        self.assertTrue(entries[0]["referral"])

    def test_post_text_is_not_used_when_post_has_several_links(self):
        # «для рефов» относится к одному из колёс — к какому, неизвестно,
        # поэтому текст поста как сигнал не используется.
        message = make_message(
            "demo/1",
            "Колесо для рефов 🔥",
            [
                "https://betboom.ru/freestream/a",
                "https://betboom.ru/freestream/b",
            ],
        )
        with patch.object(
            parser, "precheck_wheel", return_value=("active", False, "")
        ) as precheck:
            self.process(message, {})

        self.assertEqual(precheck.call_args.kwargs["post_text"], "")

    def test_expired_wheel_is_not_notified(self):
        message = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/a"])
        with patch.object(parser, "precheck_wheel", return_value=("expired", False, "")):
            self.assertEqual(self.process(message, {}), [])
        self.single.assert_not_called()

    def test_expired_wheel_is_registered_for_retry(self):
        # Пост уже помечен «увиденным» и правка его не перепроверит —
        # единственный шанс поймать ошибочный precheck (ложный is_ended,
        # неточный duration_min) — ретрай по PENDING_EXPIRED_RETRY.
        url = "https://betboom.ru/freestream/a"
        message = make_message("demo/1", "колесо", [url])
        with patch.object(parser, "precheck_wheel", return_value=("expired", False, "")):
            self.process(message, {})

        self.assertIn(url, parser.PENDING_EXPIRED_RETRY)
        info = parser.PENDING_EXPIRED_RETRY[url]
        self.assertEqual(info["channel"], "demo")
        self.assertEqual(info["msg_id"], "demo/1")

    def test_soon_wheel_is_not_notified(self):
        # Розыгрыш ещё не начался — уведомлять рано (пользователь получил
        # бы ссылку, по которой нечего делать).
        message = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/a"])
        with patch.object(parser, "precheck_wheel", return_value=("soon", False, "")):
            self.assertEqual(self.process(message, {}), [])
        self.single.assert_not_called()

    def test_soon_wheel_is_registered_for_retry(self):
        url = "https://betboom.ru/freestream/a"
        message = make_message("demo/1", "колесо", [url])
        with patch.object(parser, "precheck_wheel", return_value=("soon", False, "")):
            self.process(message, {})

        self.assertIn(url, parser.PENDING_EXPIRED_RETRY)
        info = parser.PENDING_EXPIRED_RETRY[url]
        self.assertEqual(info["channel"], "demo")
        self.assertEqual(info["msg_id"], "demo/1")

    def test_unknown_wheel_is_not_notified(self):
        # unknown — «проверить не удалось», а не «колесо живое». Уведомление
        # уходит только по явному active: сбой сети, протухшая подпись или
        # заглушка API не повод рассылать ссылку неизвестного состояния.
        message = make_message("demo/1", "колесо", ["https://betboom.ru/freestream/a"])
        with patch.object(parser, "precheck_wheel", return_value=("unknown", False, "")):
            self.assertEqual(self.process(message, {}), [])
        self.single.assert_not_called()

    def test_unknown_wheel_is_registered_for_retry(self):
        # Молчание не значит потерю: ссылка ждёт в очереди и придёт сама,
        # как только статус определится (см. retry_expired_links).
        url = "https://betboom.ru/freestream/a"
        message = make_message("demo/1", "колесо", [url])
        with patch.object(parser, "precheck_wheel", return_value=("unknown", False, "")):
            self.process(message, {})

        self.assertIn(url, parser.PENDING_EXPIRED_RETRY)
        info = parser.PENDING_EXPIRED_RETRY[url]
        self.assertEqual(info["channel"], "demo")
        self.assertEqual(info["msg_id"], "demo/1")

    def test_missing_wheel_is_not_notified_but_stays_for_retry(self):
        # 404 на странице колеса чаще всего означает выдуманный слаг, но
        # тот же ответ приходит при блокировке и сбое CDN. Уведомление не
        # шлём, а ссылку держим в очереди: цена лишнего дешёвого GET
        # несопоставима с ценой потерянного живого колеса. Мусорные
        # адреса уйдут из очереди сами по NOTIFY_RETRY_WINDOW_MINUTES.
        url = "https://betboom.ru/freestream/a"
        message = make_message("demo/1", "колесо", [url])
        with patch.object(parser, "precheck_wheel", return_value=("missing", False, "")):
            self.assertEqual(self.process(message, {}), [])

        self.single.assert_not_called()
        self.assertIn(url, parser.PENDING_EXPIRED_RETRY)

    def test_cooldown_skip_is_logged_for_diagnostics(self):
        # Раньше подавление кулдауном было немым continue: перезапуск
        # колеса на том же URL внутри REALERT_COOLDOWN_MINUTES проходил
        # без единой строки в логе, и «почему не пришло» было неоткуда
        # диагностировать.
        url = "https://betboom.ru/freestream/a"
        message = make_message("demo/1", "колесо", [url])
        last_found = {url: self.now}

        with self.assertLogs(parser.log, level="INFO") as logs:
            entries = parser.process_message(
                message, "demo", {}, False, self.now, last_found
            )

        self.assertEqual(entries, [])
        self.single.assert_not_called()
        self.assertTrue(any(url in line and "кулдаун" in line for line in logs.output))

    def test_cooldown_claimed_by_twitch_suppresses_parser_alert(self):
        # Если Twitch-поток уже занял кулдаун через claim_url_alert,
        # parser не должен слать повторное уведомление.
        url = "https://betboom.ru/freestream/a"
        message = make_message("demo/1", "колесо", [url])
        self.assertTrue(alerts.claim_url_alert(url, self.now))

        entries = self.process(message, {})

        self.assertEqual(entries, [])
        self.single.assert_not_called()

    def test_expired_wheel_does_not_block_later_restart_notification(self):
        # Пропуск expired-«хвоста» — не уведомление: кулдаун ставить нельзя,
        # иначе перезапуск того же колеса на том же адресе в пределах
        # REALERT_COOLDOWN_MINUTES останется без уведомления.
        url = "https://betboom.ru/freestream/a"
        tail_message = make_message("demo/1", "колесо", [url])
        with patch.object(parser, "precheck_wheel", return_value=("expired", False, "")):
            self.assertEqual(self.process(tail_message, {}), [])
        self.single.assert_not_called()

        # Колесо перезапущено на том же адресе (precheck_wheel из setUp
        # снова "active") — уведомление обязано уйти.
        restarted_message = make_message("demo/2", "колесо", [url])
        entries = self.process(restarted_message, {})

        self.assertEqual(len(entries), 1)
        self.single.assert_called_once()

    def test_keywords_are_checked_only_for_messages_without_links(self):
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(parser, "send_keyword_notification") as notify:
            self.process(make_message("demo/1", "будет колесо", []), {})
            notify.assert_called_once()

            notify.reset_mock()
            self.process(
                make_message("demo/2", "колесо", ["https://betboom.ru/freestream/a"]),
                {},
            )
            notify.assert_not_called()

    def test_keywords_are_checked_when_all_links_are_skipped(self):
        # Пост со ссылкой, которая отсеяна прекчеком (истёкший «хвост»),
        # не должен остаться совсем без уведомления — иначе пост пропал
        # бы молча, хотя в тексте есть ключевое слово.
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(parser, "precheck_wheel", return_value=("expired", False, "")), \
             patch.object(parser, "send_keyword_notification") as notify:
            entries = self.process(
                make_message(
                    "demo/1", "будет колесо", ["https://betboom.ru/freestream/a"]
                ),
                {},
            )

        notify.assert_called_once()
        self.single.assert_not_called()
        self.assertEqual(len(entries), 1)
        self.assertNotIn("url", entries[0])
        self.assertEqual(entries[0]["keywords"], ["колесо"])

    def test_failed_keyword_notification_is_recorded_for_retry(self):
        # Пост обрабатывается по хэшу один раз: без записи в истории
        # находка по ключевому слову терялась бы навсегда.
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(parser, "send_keyword_notification", return_value=False):
            entries = self.process(make_message("demo/1", "будет колесо", []), {})

        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["notified"])
        self.assertEqual(entries[0]["keywords"], ["колесо"])
        # Без url: это не колесо, и в /wheels, /status, /active запись не идёт.
        self.assertNotIn("url", entries[0])

    def test_delivered_keyword_notification_is_marked_notified(self):
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(parser, "send_keyword_notification", return_value=True):
            entries = self.process(make_message("demo/1", "будет колесо", []), {})

        self.assertTrue(entries[0]["notified"])

    def test_keyword_alert_dropped_for_third_party_link(self):
        # Скам-казино: пост со словом «колесо», но ссылка ведёт не на
        # betboom.ru — алерта быть не должно (см. urls.find_disallowed_domains).
        with patch.object(registry, "KEYWORDS", ["колесо"]), \
             patch.object(parser, "send_keyword_notification") as notify:
            entries = self.process(
                make_message(
                    "demo/1",
                    "Колесо на 60000$",
                    [],
                    disallowed_domains=["mellehdw.life"],
                ),
                {},
            )

        notify.assert_not_called()
        self.assertEqual(entries, [])


class RetryExpiredLinksTests(unittest.TestCase):
    """Ссылка, ошибочно признанная expired, не должна теряться навсегда:
    её пост уже помечен «увиденным», и правка поста её не перепроверит —
    единственный путь назад — retry_expired_links (см. PENDING_EXPIRED_RETRY)."""

    def setUp(self):
        self.addCleanup(parser.PENDING_EXPIRED_RETRY.clear)
        parser.PENDING_EXPIRED_RETRY.clear()
        self.addCleanup(alerts.LAST_URL_ALERT.clear)
        alerts.LAST_URL_ALERT.clear()
        self.now = parser.now_msk()
        self.url = "https://betboom.ru/freestream/a"

    def _seed(self, **overrides):
        info = {
            "channel": "demo",
            "msg_id": "demo/1",
            "message_url": "https://t.me/demo/1",
            "preview": "колесо",
            "post_text": "колесо",
            "first_seen": self.now,
        }
        info.update(overrides)
        parser.PENDING_EXPIRED_RETRY[self.url] = info

    def test_empty_registry_is_a_cheap_noop(self):
        with patch.object(parser, "precheck_wheel") as precheck:
            entries = parser.retry_expired_links(self.now, {})
        precheck.assert_not_called()
        self.assertEqual(entries, [])

    def test_wheel_recovered_from_expired_is_notified(self):
        self._seed()
        with patch.object(
            parser, "precheck_wheel", return_value=("active", False, "")
        ) as precheck, patch.object(
            parser, "send_telegram_notification", return_value=True
        ) as send:
            entries = parser.retry_expired_links(self.now, {})

        # Ретрай обязан обходить expired-кэш betboom.py — иначе он раз за
        # разом получал бы старый статус из кэша, ни разу не дойдя до API
        # (см. config.EXPIRED_CACHE_TTL_SECONDS).
        self.assertEqual(precheck.call_args.kwargs.get("use_cache"), False)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["url"], self.url)
        self.assertEqual(entries[0]["status"], "active")
        self.assertTrue(entries[0]["notified"])
        send.assert_called_once()
        self.assertNotIn(self.url, parser.PENDING_EXPIRED_RETRY)
        # Кулдаун должен встать — иначе обычный цикл тут же продублирует.
        self.assertTrue(alerts.cooldown_active(self.url, self.now))

    def test_unknown_status_does_not_resurrect_link(self):
        # unknown — это «проверить не удалось», а не «ожило». Ссылка сюда
        # попала уже отвергнутой, нового поста за ней нет, и оповещать по
        # отсутствию информации значит рассылать мертвецов: именно так
        # заглушка API BetBoom (все статусы unknown) превращала весь
        # накопленный список в пачку устаревших уведомлений после рестарта.
        self._seed()
        with patch.object(
            parser, "precheck_wheel", return_value=("unknown", False, "")
        ), patch.object(parser, "send_telegram_notification") as send:
            entries = parser.retry_expired_links(self.now, {})

        self.assertEqual(entries, [])
        send.assert_not_called()
        # Ссылка остаётся на перепроверке: вдруг API оживёт до конца окна.
        self.assertIn(self.url, parser.PENDING_EXPIRED_RETRY)
        self.assertFalse(alerts.cooldown_active(self.url, self.now))

    def test_retry_recovers_wheel_through_real_expired_cache(self):
        """Регрессия: precheck_wheel отдавал expired из кэша betboom.py, и
        ретрай ни разу не доходил до настоящего API, пока кэш не протухал.
        Здесь precheck_wheel НЕ мокается — используется реальная функция с
        реальным кэшем (TTL — EXPIRED_CACHE_TTL_SECONDS, 120 c), чтобы
        проверить интеграцию, а не только то, что retry_expired_links передаёт
        нужный флаг."""
        self._seed()
        self.addCleanup(betboom._expired_cache.clear)
        # Полный ответ API, а не только is_ended: заглушку betboom опознаёт по
        # отсутствию action_uid и отдаёт unknown, так что ответ без него
        # статусом не считается (см. betboom.api_info_to_status).
        ended = dt.now(timezone.utc) - timedelta(hours=2)
        expired_info = {
            "action_uid": "action-uid-1",
            "is_ended": True,
            "is_early": False,
            "start_dttm": ended.isoformat().replace("+00:00", "Z"),
            "duration_min": 30,
        }
        with patch.object(betboom, "fetch_wheel_info", return_value=expired_info):
            # Кэш выставляется так же, как при первичном обнаружении «хвоста».
            self.assertEqual(betboom.precheck_wheel(self.url)[0], "expired")

        started = dt.now(timezone.utc) - timedelta(minutes=1)
        active_info = {
            "action_uid": "action-uid-1",
            "is_ended": False,
            "is_early": False,
            "start_dttm": started.isoformat().replace("+00:00", "Z"),
            "duration_min": 30,
        }
        with patch.object(
            betboom, "fetch_wheel_info", return_value=active_info
        ), patch.object(
            parser, "send_telegram_notification", return_value=True
        ) as send:
            entries = parser.retry_expired_links(self.now, {})

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "active")
        send.assert_called_once()

    def test_still_expired_wheel_stays_registered_without_notifying(self):
        self._seed()
        with patch.object(
            parser, "precheck_wheel", return_value=("expired", False, "")
        ), patch.object(parser, "send_telegram_notification") as send:
            entries = parser.retry_expired_links(self.now, {})

        self.assertEqual(entries, [])
        send.assert_not_called()
        self.assertIn(self.url, parser.PENDING_EXPIRED_RETRY)

    def test_wheel_recovered_from_soon_is_notified(self):
        self._seed()
        with patch.object(
            parser, "precheck_wheel", return_value=("active", False, "")
        ), patch.object(
            parser, "send_telegram_notification", return_value=True
        ) as send:
            entries = parser.retry_expired_links(self.now, {})

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["status"], "active")
        self.assertTrue(entries[0]["notified"])
        send.assert_called_once()
        self.assertNotIn(self.url, parser.PENDING_EXPIRED_RETRY)

    def test_still_soon_wheel_stays_registered_without_notifying(self):
        self._seed()
        with patch.object(
            parser, "precheck_wheel", return_value=("soon", False, "")
        ), patch.object(parser, "send_telegram_notification") as send:
            entries = parser.retry_expired_links(self.now, {})

        self.assertEqual(entries, [])
        send.assert_not_called()
        self.assertIn(self.url, parser.PENDING_EXPIRED_RETRY)

    def test_stale_entry_beyond_retry_window_is_dropped_without_notifying(self):
        stale_first_seen = self.now - timedelta(
            minutes=config.NOTIFY_RETRY_WINDOW_MINUTES + 1
        )
        self._seed(first_seen=stale_first_seen)
        with patch.object(parser, "precheck_wheel") as precheck, patch.object(
            parser, "send_telegram_notification"
        ) as send:
            entries = parser.retry_expired_links(self.now, {})

        precheck.assert_not_called()
        send.assert_not_called()
        self.assertEqual(entries, [])
        self.assertNotIn(self.url, parser.PENDING_EXPIRED_RETRY)

    def test_entry_already_covered_by_cooldown_is_dropped_without_duplicate(self):
        # Уведомление об этом URL уже ушло из другого источника (Twitch,
        # обычный ретрай доставки), пока ссылка ждала перепроверки.
        self._seed()
        with patch.object(
            parser, "_is_on_cooldown", return_value=True
        ), patch.object(parser, "precheck_wheel") as precheck, patch.object(
            parser, "send_telegram_notification"
        ) as send, self.assertLogs(parser.log, level="INFO") as logs:
            entries = parser.retry_expired_links(self.now, {})

        precheck.assert_not_called()
        send.assert_not_called()
        self.assertEqual(entries, [])
        self.assertNotIn(self.url, parser.PENDING_EXPIRED_RETRY)
        # Подавление не должно быть молчаливым — иначе «почему не пришло»
        # диагностировать неоткуда.
        self.assertTrue(any(self.url in line for line in logs.output))


class TwitchRetryDrainTests(unittest.TestCase):
    """twitch-worker не пишет в PENDING_EXPIRED_RETRY напрямую (см.
    storage.py — словарь и файл без лока): он кладёт заявку в
    twitch.TWITCH_PENDING_RETRY, а parser регистрирует её на ретрай в
    начале своего цикла, как и находки из TWITCH_NEW_ENTRIES."""

    def setUp(self):
        self.addCleanup(parser.PENDING_EXPIRED_RETRY.clear)
        parser.PENDING_EXPIRED_RETRY.clear()
        while not twitch.TWITCH_PENDING_RETRY.empty():
            twitch.TWITCH_PENDING_RETRY.get_nowait()
        self.addCleanup(self._drain_leftovers)
        self.now = parser.now_msk()

    def _drain_leftovers(self):
        while not twitch.TWITCH_PENDING_RETRY.empty():
            twitch.TWITCH_PENDING_RETRY.get_nowait()

    def test_queued_job_is_registered_for_retry(self):
        url = "https://betboom.ru/freestream/a"
        twitch.TWITCH_PENDING_RETRY.put({
            "url": url,
            "channel": "aunkere",
            "message": {
                "id": "abc123",
                "message_url": "https://www.twitch.tv/aunkere",
                "text": f"колесо {url}",
            },
            "post_text": f"колесо {url}",
            "now": self.now,
        })

        parser.drain_twitch_retry_registrations()

        self.assertIn(url, parser.PENDING_EXPIRED_RETRY)
        info = parser.PENDING_EXPIRED_RETRY[url]
        self.assertEqual(info["channel"], "aunkere")
        self.assertEqual(info["msg_id"], "abc123")
        self.assertEqual(info["message_url"], "https://www.twitch.tv/aunkere")

    def test_empty_queue_is_a_cheap_noop(self):
        parser.drain_twitch_retry_registrations()
        self.assertEqual(parser.PENDING_EXPIRED_RETRY, {})


class PendingExpiredPersistenceTests(unittest.TestCase):
    """PENDING_EXPIRED_RETRY обязан переживать рестарт процесса: пост, чья
    ссылка ошибочно признана expired, уже помечен «увиденным» в
    seen_ids.json — обычная правка поста больше не даст шанса на
    перепроверку, единственный путь назад — восстановление этого списка
    из pending_expired.json при старте (см. app.main)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wheelsparser-pending-parser-"))
        patcher = patch.object(storage, "PENDING_EXPIRED_FILE", self.tmp / "pending_expired.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(parser.PENDING_EXPIRED_RETRY.clear)
        parser.PENDING_EXPIRED_RETRY.clear()
        self.url = "https://betboom.ru/freestream/a"
        self.message = make_message("demo/1", "колесо", [self.url])
        self.now = parser.now_msk()

    def test_registered_entry_survives_simulated_restart(self):
        parser._register_pending_expired(
            self.url, "demo", self.message, "колесо", self.now
        )

        # Симулируем рестарт процесса: очищаем память и грузим заново с диска
        # (то же самое делает app.main() через load_pending_expired_retry()).
        parser.PENDING_EXPIRED_RETRY.clear()
        parser.load_pending_expired_retry()

        self.assertIn(self.url, parser.PENDING_EXPIRED_RETRY)
        restored = parser.PENDING_EXPIRED_RETRY[self.url]
        self.assertEqual(restored["channel"], "demo")
        self.assertEqual(restored["msg_id"], "demo/1")
        self.assertEqual(restored["post_text"], "колесо")

    def test_dropped_entry_does_not_come_back_after_restart(self):
        parser._register_pending_expired(
            self.url, "demo", self.message, "колесо", self.now
        )
        parser._drop_pending_expired(self.url)

        parser.PENDING_EXPIRED_RETRY.clear()
        parser.load_pending_expired_retry()

        self.assertEqual(parser.PENDING_EXPIRED_RETRY, {})

    def test_registering_same_url_twice_keeps_first_seen(self):
        # setdefault-семантика: вторая попытка (например, другой канал
        # репостнул тот же «хвост») не должна сдвигать окно ретрая.
        parser._register_pending_expired(
            self.url, "demo", self.message, "колесо", self.now
        )
        later = self.now + timedelta(minutes=5)
        other_message = make_message("other/1", "колесо", [self.url])
        parser._register_pending_expired(
            self.url, "other", other_message, "колесо", later
        )

        self.assertEqual(parser.PENDING_EXPIRED_RETRY[self.url]["channel"], "demo")
        self.assertEqual(parser.PENDING_EXPIRED_RETRY[self.url]["first_seen"], self.now)


class RetryFailedNotificationsTests(unittest.TestCase):
    """Пост обрабатывается по хэшу один раз — сбой отправки без ретрая
    терял бы находку навсегда, поэтому retry_failed_notifications должен
    подбирать из базы записи с notified=0 на следующих циклах."""

    def _start(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def setUp(self):
        use_temp_db(self)
        self._start(patch.object(parser, "notifications_enabled", return_value=True))
        self._start(patch.dict(parser.RETRY_ATTEMPT_COUNTS, clear=True))
        self.now = parser.now_msk()

    def store(self, url="https://betboom.ru/freestream/a", notified=False,
              age_minutes=1, **overrides):
        """Кладёт запись в базу и возвращает её (с проставленным id)."""
        entry = {
            "url": url,
            "found_at": (self.now - timedelta(minutes=age_minutes)).isoformat(
                timespec="seconds"
            ),
            "channel": "demo",
            "notified": notified,
        }
        entry.update(overrides)
        db.insert_entries([entry])
        return entry

    def pending_urls(self):
        window = self.now - timedelta(minutes=config.NOTIFY_RETRY_WINDOW_MINUTES)
        return [item.get("url") for item in db.pending_retry(window, 100)]

    def test_retries_and_marks_notified_on_success(self):
        send = self._start(
            patch.object(parser, "send_telegram_notification", return_value=True)
        )
        self.store()

        retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, 1)
        send.assert_called_once()

    def test_successful_retry_is_persisted_and_not_repeated(self):
        # Без записи в базу следующий цикл отправил бы то же уведомление снова.
        self._start(
            patch.object(parser, "send_telegram_notification", return_value=True)
        )
        self.store()

        parser.retry_failed_notifications(self.now)

        self.assertEqual(self.pending_urls(), [])

    def test_already_notified_entries_are_skipped(self):
        send = self._start(patch.object(parser, "send_telegram_notification"))
        self.store(notified=True)

        retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, 0)
        send.assert_not_called()

    def test_entries_older_than_window_are_not_retried(self):
        send = self._start(patch.object(parser, "send_telegram_notification"))
        self.store(age_minutes=config.NOTIFY_RETRY_WINDOW_MINUTES + 1)

        retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, 0)
        send.assert_not_called()

    def test_stays_pending_when_retry_also_fails(self):
        self._start(patch.object(parser, "send_telegram_notification", return_value=False))
        self.store()

        parser.retry_failed_notifications(self.now)

        self.assertEqual(self.pending_urls(), ["https://betboom.ru/freestream/a"])

    def test_entries_with_unknown_delivery_are_not_retried(self):
        # Telegram мог принять сообщение и не донести ответ — повтор
        # такой отправки рассылает дубликат.
        send = self._start(patch.object(parser, "send_telegram_notification"))
        self.store(delivery_unknown=True)

        retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, 0)
        send.assert_not_called()

    def test_unknown_delivery_during_retry_stops_further_attempts(self):
        # Отправка могла дойти, а ответ — нет: флаг должен попасть в базу,
        # иначе следующий цикл продублирует сообщение.
        def mark_unknown(entry, *_args, **_kwargs):
            entry["delivery_unknown"] = True
            return False

        self._start(
            patch.object(parser, "send_telegram_notification", side_effect=mark_unknown)
        )
        self.store()

        parser.retry_failed_notifications(self.now)

        self.assertEqual(self.pending_urls(), [])

    def test_respects_max_per_cycle_limit(self):
        send = self._start(
            patch.object(parser, "send_telegram_notification", return_value=True)
        )
        for index in range(config.NOTIFY_RETRY_MAX_PER_CYCLE + 3):
            self.store(url=f"https://betboom.ru/freestream/{index}")

        retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, config.NOTIFY_RETRY_MAX_PER_CYCLE)
        self.assertEqual(send.call_count, config.NOTIFY_RETRY_MAX_PER_CYCLE)

    def test_stuck_entry_does_not_permanently_starve_newer_entries(self):
        # Раньше ORDER BY found_at без учёта числа попыток означал: самая
        # старая запись всегда первая, и если она не доставляется раз за
        # разом, она монополизирует весь NOTIFY_RETRY_MAX_PER_CYCLE — более
        # свежая запись не получает ни одной попытки, пока «застрявшие» не
        # выйдут из окна ретрая естественным путём (до NOTIFY_RETRY_WINDOW_MINUTES).
        for index in range(config.NOTIFY_RETRY_MAX_PER_CYCLE):
            self.store(url=f"https://betboom.ru/freestream/stuck{index}", age_minutes=10)
        fresh_url = "https://betboom.ru/freestream/fresh"
        self.store(url=fresh_url, age_minutes=1)

        def flaky(entry, *_args, **_kwargs):
            return "stuck" not in entry["url"]

        self._start(patch.object(parser, "send_telegram_notification", side_effect=flaky))

        # Цикл 1: пул полностью укомплектован «застрявшими» — все записи
        # ещё с одинаковым числом попыток (0), порядок по found_at решает,
        # а «застрявшие» старше. Свежая ещё не пробуется.
        parser.retry_failed_notifications(self.now)
        self.assertIn(fresh_url, self.pending_urls())

        # Цикл 2: «застрявшие» набрали по попытке, у свежей попыток всё
        # ещё ноль — она идёт первой в сортировке и получает шанс.
        parser.retry_failed_notifications(self.now)
        self.assertNotIn(fresh_url, self.pending_urls())

    def test_attempt_count_increments_on_failure_and_clears_on_success(self):
        entry = self.store()
        entry_id = entry["id"]

        with patch.object(parser, "send_telegram_notification", return_value=False):
            parser.retry_failed_notifications(self.now)
        self.assertEqual(parser.RETRY_ATTEMPT_COUNTS.get(entry_id), 1)

        with patch.object(parser, "send_telegram_notification", return_value=True):
            parser.retry_failed_notifications(self.now)
        self.assertNotIn(entry_id, parser.RETRY_ATTEMPT_COUNTS)

    def test_retries_keyword_notification_without_url(self):
        # У находок по ключевым словам url нет — ретрай узнаёт их по
        # keywords и шлёт своим отправителем.
        keyword_send = self._start(
            patch.object(parser, "send_keyword_notification", return_value=True)
        )
        link_send = self._start(patch.object(parser, "send_telegram_notification"))
        self.store(
            url="",
            keywords=["колесо"],
            message_url="https://t.me/demo/1",
        )

        retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, 1)
        self.assertEqual(self.pending_urls(), [])
        keyword_send.assert_called_once()
        link_send.assert_not_called()

    def test_entries_without_url_and_keywords_are_skipped(self):
        keyword_send = self._start(patch.object(parser, "send_keyword_notification"))
        link_send = self._start(patch.object(parser, "send_telegram_notification"))
        self.store(url="")

        retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, 0)
        keyword_send.assert_not_called()
        link_send.assert_not_called()

    def test_noop_when_notifications_disabled(self):
        with patch.object(parser, "notifications_enabled", return_value=False):
            send = self._start(patch.object(parser, "send_telegram_notification"))
            self.store()

            retried = parser.retry_failed_notifications(self.now)

        self.assertEqual(retried, 0)
        send.assert_not_called()


class EmptyChannelDetectionTests(unittest.TestCase):
    """Страница канала отдалась, но постов в ней нет.

    Единственный отказ, который иначе не виден: канал засчитывается
    успешным, в логе «каналов N/N», новых ссылок ноль — и так до тех пор,
    пока кто-нибудь не заметит, что колёса перестали приходить.
    """

    def _start(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def setUp(self):
        self._start(patch.dict(parser.CHANNEL_EMPTY_STREAK, clear=True))
        self._start(patch.object(parser, "CHANNEL_EMPTY_ALERTED", set()))
        self._start(patch.object(parser, "LAYOUT_ALERTED", False))
        self._start(patch.object(parser, "CHANNEL_EMPTY_THRESHOLD", 3))
        self.notify = self._start(patch.object(parser, "send_service_notification"))

    def run_cycles(self, checked, failed, empty, times=1):
        for _ in range(times):
            parser.update_channel_empty_streaks(checked, failed, empty)
            parser.report_empty_channels(checked, failed)

    def test_streak_grows_and_alerts_only_at_threshold(self):
        with patch.object(registry, "CHANNELS", ["a", "b"]):
            self.run_cycles(["a", "b"], [], ["a"], times=2)
            self.notify.assert_not_called()

            self.run_cycles(["a", "b"], [], ["a"])

        self.notify.assert_called_once()
        self.assertIn("@a", self.notify.call_args.args[0])

    def test_alert_is_sent_once_per_series(self):
        with patch.object(registry, "CHANNELS", ["a", "b"]):
            self.run_cycles(["a", "b"], [], ["a"], times=5)

        self.notify.assert_called_once()

    def test_posts_reset_streak_and_allow_new_alert(self):
        with patch.object(registry, "CHANNELS", ["a", "b"]):
            self.run_cycles(["a", "b"], [], ["a"], times=3)
            self.assertEqual(self.notify.call_count, 1)

            self.run_cycles(["a", "b"], [], [])  # канал снова отдал посты
            self.assertEqual(parser.CHANNEL_EMPTY_STREAK.get("a", 0), 0)

            self.run_cycles(["a", "b"], [], ["a"], times=3)

        self.assertEqual(self.notify.call_count, 2)

    def test_unreachable_channel_is_not_counted_as_empty(self):
        """Недоступность — забота fail-streak, смешивать счётчики нельзя."""
        with patch.object(registry, "CHANNELS", ["a", "b"]):
            self.run_cycles(["a", "b"], ["a"], [], times=5)

        self.notify.assert_not_called()
        self.assertNotIn("a", parser.CHANNEL_EMPTY_STREAK)

    def test_all_channels_empty_reports_layout_change_once(self):
        with patch.object(registry, "CHANNELS", ["a", "b", "c"]):
            self.run_cycles(["a", "b", "c"], [], ["a", "b", "c"], times=4)

        # Одно сообщение про вёрстку, а не по одному на каждый канал.
        self.notify.assert_called_once()
        message = self.notify.call_args.args[0]
        self.assertIn("вёрстка t.me/s", message)

    def test_layout_alert_repeats_after_recovery(self):
        with patch.object(registry, "CHANNELS", ["a", "b"]):
            self.run_cycles(["a", "b"], [], ["a", "b"], times=3)
            self.assertEqual(self.notify.call_count, 1)

            self.run_cycles(["a", "b"], [], [])  # разбор починился
            self.run_cycles(["a", "b"], [], ["a", "b"], times=3)

        self.assertEqual(self.notify.call_count, 2)

    def test_single_channel_setup_reports_channel_not_layout(self):
        """С одним каналом «сломалась вёрстка» и «канал опустел» неразличимы."""
        with patch.object(registry, "CHANNELS", ["a"]):
            self.run_cycles(["a"], [], ["a"], times=3)

        self.notify.assert_called_once()
        self.assertIn("@a", self.notify.call_args.args[0])

    def test_streaks_of_removed_channels_are_dropped(self):
        with patch.object(registry, "CHANNELS", ["a", "b"]):
            self.run_cycles(["a", "b"], [], ["a"], times=2)
        with patch.object(registry, "CHANNELS", ["b"]):  # /remove a
            self.run_cycles(["b"], [], [])

        self.assertNotIn("a", parser.CHANNEL_EMPTY_STREAK)

    def test_process_cycle_counts_channel_without_posts_as_empty(self):
        use_temp_db(self)
        seen: dict[str, dict[str, str]] = {}
        with patch.dict(parser.CHANNEL_EMPTY_STREAK, clear=True), \
             patch.object(registry, "CHANNELS", ["a", "b"]), \
             patch.object(parser, "fetch_channel", return_value=[]), \
             patch.object(parser, "save_seen"):
            parser.process_cycle(seen, baseline=True)

            self.assertEqual(parser.CHANNEL_EMPTY_STREAK, {"a": 1, "b": 1})

    def test_process_cycle_does_not_count_unreachable_channel_as_empty(self):
        use_temp_db(self)
        seen: dict[str, dict[str, str]] = {}
        with patch.dict(parser.CHANNEL_EMPTY_STREAK, clear=True), \
             patch.object(registry, "CHANNELS", ["a"]), \
             patch.object(parser, "fetch_channel", return_value=None), \
             patch.object(parser, "save_seen"):
            parser.process_cycle(seen, baseline=True)

            self.assertEqual(parser.CHANNEL_EMPTY_STREAK, {})


class ProcessCycleTests(unittest.TestCase):
    def setUp(self):
        use_temp_db(self)
        self.now = parser.now_msk()

    def stored(self):
        return entries_since(self.now - timedelta(hours=1))

    def test_found_wheel_is_written_to_the_database(self):
        message = make_message(
            "demo/1", "колесо", ["https://betboom.ru/freestream/new"]
        )

        with patch.dict(alerts.LAST_URL_ALERT, clear=True), \
             patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(parser, "precheck_wheel", return_value=("active", False, "")), \
             patch.object(parser, "fetch_channel", return_value=[message]), \
             patch.object(parser, "send_telegram_notification", return_value=True), \
             patch.object(parser, "save_seen"):
            # Непустой seen: у канала уже есть история, значит «тихий»
            # первый цикл ему не положен и уведомление уходит сразу.
            parser.process_cycle({"demo": {"demo/0": "hash"}}, baseline=False)

        (entry,) = self.stored()
        self.assertEqual(entry["url"], "https://betboom.ru/freestream/new")
        self.assertEqual(entry["channel"], "demo")
        self.assertTrue(entry["notified"])

    def test_one_channel_crash_does_not_abort_the_whole_cycle(self):
        # _fetch_all_channels опрашивает каналы через ThreadPoolExecutor:
        # необработанное исключение при разборе одного канала не должно
        # унести с собой проверку остальных каналов этого же цикла.
        message = make_message(
            "demo/1", "колесо", ["https://betboom.ru/freestream/ok"]
        )

        def flaky_fetch(channel, session=None):
            if channel == "broken":
                raise RuntimeError("boom")
            return [message]

        with patch.dict(parser.CHANNEL_FAIL_STREAK, clear=True), \
             patch.object(parser, "CHANNEL_FAIL_ALERTED", set()), \
             patch.dict(alerts.LAST_URL_ALERT, clear=True), \
             patch.object(registry, "CHANNELS", ["broken", "demo"]), \
             patch.object(parser, "precheck_wheel", return_value=("active", False, "")), \
             patch.object(parser, "fetch_channel", side_effect=flaky_fetch), \
             patch.object(parser, "send_telegram_notification", return_value=True), \
             patch.object(parser, "save_seen"):
            parser.process_cycle(
                {"broken": {"broken/0": "hash"}, "demo": {"demo/0": "hash"}},
                baseline=False,
            )

        (entry,) = self.stored()
        self.assertEqual(entry["channel"], "demo")
        self.assertEqual(entry["url"], "https://betboom.ru/freestream/ok")

    def test_db_write_failure_for_one_message_does_not_abort_the_cycle(self):
        # Сбой самой записи в базу (например, sqlite залочена дольше
        # busy_timeout) не должен прерывать обработку остальных каналов
        # этого цикла — иначе одна редкая ошибка теряет куда больше
        # находок, чем одну.
        broken_message = make_message(
            "broken/1", "колесо", ["https://betboom.ru/freestream/lost"]
        )
        ok_message = make_message(
            "demo/1", "колесо", ["https://betboom.ru/freestream/ok"]
        )
        real_insert = db.insert_entries

        def flaky_insert(entries):
            if any(entry.get("channel") == "broken" for entry in entries):
                raise sqlite3.OperationalError("database is locked")
            real_insert(entries)

        def fetch_by_channel(channel, session=None):
            return [broken_message] if channel == "broken" else [ok_message]

        seen = {"broken": {"broken/0": "hash"}, "demo": {"demo/0": "hash"}}
        with patch.dict(alerts.LAST_URL_ALERT, clear=True), \
             patch.object(registry, "CHANNELS", ["broken", "demo"]), \
             patch.object(parser, "precheck_wheel", return_value=("active", False, "")), \
             patch.object(parser, "fetch_channel", side_effect=fetch_by_channel), \
             patch.object(parser, "send_telegram_notification", return_value=True), \
             patch.object(db, "insert_entries", side_effect=flaky_insert), \
             patch.object(parser, "save_seen") as save_seen_mock:
            parser.process_cycle(seen, baseline=False)

        # Демо-канал, обработанный ПОСЛЕ сбойного, всё равно записан.
        (entry,) = self.stored()
        self.assertEqual(entry["channel"], "demo")
        self.assertEqual(entry["url"], "https://betboom.ru/freestream/ok")
        # Цикл доехал до конца, а не оборвался на сбойной записи.
        save_seen_mock.assert_called_once()
        # Пост уже помечен «увиденным» (уведомление, если было, уже
        # отправлено в Telegram) — откатывать эту пометку нельзя, иначе
        # повтор на следующем цикле продублировал бы отправку.
        self.assertIn("broken/1", seen["broken"])

    def test_twitch_db_write_failure_does_not_block_channel_checks(self):
        ok_message = make_message(
            "demo/1", "колесо", ["https://betboom.ru/freestream/ok"]
        )
        parser.TWITCH_NEW_ENTRIES.put({
            "url": "https://betboom.ru/freestream/twitch-lost",
            "found_at": self.now.isoformat(timespec="seconds"),
            "channel": "streamer",
            "source": "twitch",
            "notified": True,
        })
        real_insert = db.insert_entries

        def flaky_insert(entries):
            if any(entry.get("source") == "twitch" for entry in entries):
                raise sqlite3.OperationalError("database is locked")
            real_insert(entries)

        with patch.dict(alerts.LAST_URL_ALERT, clear=True), \
             patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(parser, "precheck_wheel", return_value=("active", False, "")), \
             patch.object(parser, "fetch_channel", return_value=[ok_message]), \
             patch.object(parser, "send_telegram_notification", return_value=True), \
             patch.object(db, "insert_entries", side_effect=flaky_insert), \
             patch.object(parser, "save_seen"):
            parser.process_cycle({"demo": {"demo/0": "hash"}}, baseline=False)

        (entry,) = self.stored()
        self.assertEqual(entry["channel"], "demo")
        self.assertEqual(entry["url"], "https://betboom.ru/freestream/ok")

    def test_same_url_from_two_new_messages_is_saved_once(self):
        first = make_message("demo/2", "колесо", ["https://betboom.ru/freestream/same"])
        second = make_message("demo/3", "колесо", ["https://betboom.ru/freestream/same"])
        seen: dict[str, dict[str, str]] = {"demo": {}}

        with patch.dict(alerts.LAST_URL_ALERT, clear=True), \
             patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(parser, "precheck_wheel", return_value=("active", False, "")), \
             patch.object(parser, "fetch_channel", side_effect=[[first], [second]]), \
             patch.object(parser, "send_telegram_notification", return_value=True), \
             patch.object(parser, "save_seen"):
            parser.process_cycle(seen, baseline=True)
            parser.process_cycle(seen, baseline=False)

        stored = self.stored()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0]["url"], "https://betboom.ru/freestream/same")

    def test_cooldown_survives_restart_because_history_is_in_the_database(self):
        # Кулдаун восстанавливается запросом к базе, а не из списка в
        # памяти: после рестарта та же ссылка не должна уйти повторно.
        message = make_message(
            "demo/9", "колесо", ["https://betboom.ru/freestream/again"]
        )
        db.insert_entries([{
            "url": "https://betboom.ru/freestream/again",
            "found_at": (self.now - timedelta(minutes=1)).isoformat(timespec="seconds"),
            "channel": "demo",
            "notified": True,
        }])

        with patch.dict(alerts.LAST_URL_ALERT, clear=True), \
             patch.object(registry, "CHANNELS", ["demo"]), \
             patch.object(parser, "precheck_wheel", return_value=("active", False, "")), \
             patch.object(parser, "fetch_channel", return_value=[message]), \
             patch.object(parser, "send_telegram_notification", return_value=True) as send, \
             patch.object(parser, "save_seen"):
            parser.process_cycle({"demo": {}}, baseline=False)

        send.assert_not_called()
        self.assertEqual(len(self.stored()), 1)

    def test_twitch_findings_from_the_queue_are_stored(self):
        parser.TWITCH_NEW_ENTRIES.put({
            "url": "https://betboom.ru/freestream/twitch",
            "found_at": self.now.isoformat(timespec="seconds"),
            "channel": "streamer",
            "source": "twitch",
            "notified": True,
        })

        with patch.object(registry, "CHANNELS", []), \
             patch.object(parser, "save_seen"):
            parser.process_cycle({}, baseline=False)

        (entry,) = self.stored()
        self.assertEqual(entry["source"], "twitch")

    def test_cycle_trims_history_to_max_results(self):
        for index in range(3):
            db.insert_entries([{
                "url": f"https://betboom.ru/freestream/{index}",
                "found_at": (self.now - timedelta(minutes=10 - index)).isoformat(
                    timespec="seconds"
                ),
                "channel": "demo",
                "notified": True,
            }])
        # Обрезка выполняется в циклах с находкой: новая приходит из Twitch.
        parser.TWITCH_NEW_ENTRIES.put({
            "url": "https://betboom.ru/freestream/fresh",
            "found_at": self.now.isoformat(timespec="seconds"),
            "channel": "streamer",
            "source": "twitch",
            "notified": True,
        })

        with patch.object(registry, "CHANNELS", []), \
             patch.object(parser, "MAX_RESULTS", 2), \
             patch.object(parser, "save_seen"):
            parser.process_cycle({}, baseline=False)

        self.assertEqual(
            [entry["url"] for entry in self.stored()],
            ["https://betboom.ru/freestream/2", "https://betboom.ru/freestream/fresh"],
        )


class FetchChannelsParallelTests(unittest.TestCase):
    def test_fetch_aborts_immediately_when_stop_event_is_set(self):
        with patch.object(parser.STOP_EVENT, "is_set", return_value=True), \
             patch.object(parser, "fetch_channel") as fetch:
            results = list(parser._fetch_all_channels(["ch1", "ch2"]))

        fetch.assert_not_called()
        self.assertEqual(sorted(results), [("ch1", None), ("ch2", None)])

    def test_ready_channel_is_yielded_before_slow_one_finishes(self):
        # Смысл потоковой отдачи: обработка (и уведомление) по быстрому
        # каналу не ждёт, пока догрузится медленный.
        released = threading.Event()

        def fetch(channel, session=None):
            if channel == "slow":
                released.wait(5)
            return []

        with patch.object(parser, "fetch_channel", side_effect=fetch):
            with closing(parser._fetch_all_channels(["slow", "fast"])) as stream:
                first_channel, _ = next(stream)
                released.set()
                rest = [channel for channel, _ in stream]

        self.assertEqual(first_channel, "fast")
        self.assertEqual(rest, ["slow"])


if __name__ == "__main__":
    unittest.main()
