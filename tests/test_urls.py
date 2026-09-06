import unittest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

from bs4 import BeautifulSoup

from wheelsparser import config, urls


class NormalizeUrlTests(unittest.TestCase):
    def test_canonicalizes_host_query_and_trailing_slash(self):
        self.assertEqual(
            urls.normalize_url(
                "https://WWW.BETBOOM.RU/freestream/demo/?utm_source=test#part"
            ),
            "https://betboom.ru/freestream/demo",
        )

    def test_unescapes_html_entities(self):
        self.assertEqual(
            urls.normalize_url("https://betboom.ru/freestream/demo&amp;x=1"),
            "https://betboom.ru/freestream/demo&x=1",
        )

    def test_adds_missing_scheme(self):
        # Twitch-боты (nightbot, StreamElements) нередко режут https:// в
        # сообщениях чата — без достройки схемы urlsplit принял бы весь
        # адрес за path, и та же ссылка из Telegram и Twitch перестала бы
        # быть одной канонической строкой (см. config.FREESTREAM_RE).
        self.assertEqual(
            urls.normalize_url("betboom.ru/freestream/demo/"),
            "https://betboom.ru/freestream/demo",
        )

    def test_adds_missing_scheme_for_www_host(self):
        self.assertEqual(
            urls.normalize_url("WWW.BETBOOM.RU/freestream/demo"),
            "https://betboom.ru/freestream/demo",
        )

    def test_schemeless_and_schemeful_urls_normalize_identically(self):
        # Дедупликация, кулдаун и expired-кэш держатся на равенстве этой
        # строки для одного и того же колеса независимо от источника.
        self.assertEqual(
            urls.normalize_url("betboom.ru/freestream/demo"),
            urls.normalize_url("https://betboom.ru/freestream/demo"),
        )

    def test_http_scheme_normalizes_to_https(self):
        # Telegram кладёт в href схему http, а видимый текст той же ссылки —
        # без схемы (достраивается выше до https). Без унификации одна и та
        # же ссылка поста давала бы два разных канонических URL: список
        # ссылок поста «плыл», хэш содержимого менялся, и правка поста
        # определялась ложно (регресс: 4 старых поста в @whylollybet разом
        # посчитались отредактированными и ушли повторно). API BetBoom к
        # тому же не отвечает на http-вариант — он молча уходил в unknown.
        self.assertEqual(
            urls.normalize_url("http://betboom.ru/freestream/demo"),
            "https://betboom.ru/freestream/demo",
        )

    def test_legacy_normalization_keeps_query(self):
        self.assertEqual(
            urls.legacy_normalize_url(
                "https://betboom.ru/freestream/demo?utm_source=test#part"
            ),
            "https://betboom.ru/freestream/demo?utm_source=test",
        )


class IsBetboomHostTests(unittest.TestCase):
    def test_betboom_ru_is_accepted(self):
        self.assertTrue(urls.is_betboom_host("https://betboom.ru/freestream/demo"))
        self.assertTrue(urls.is_betboom_host("betboom.ru/freestream/demo"))
        self.assertTrue(urls.is_betboom_host("https://www.betboom.ru/freestream/demo"))
        self.assertTrue(urls.is_betboom_host("https://sub.betboom.ru/freestream/demo"))
        self.assertTrue(urls.is_betboom_host("https://betboom.ru:443/freestream/demo"))
        self.assertTrue(urls.is_betboom_host("https://sub.betboom.ru:8443/freestream/demo"))
        self.assertTrue(urls.is_betboom_host("https://user:pass@betboom.ru/freestream/demo"))

    def test_third_party_hosts_are_rejected(self):
        self.assertFalse(urls.is_betboom_host("https://t.me/channel"))
        self.assertFalse(urls.is_betboom_host("https://vk.com/wall"))
        self.assertFalse(urls.is_betboom_host("https://notbetboom.ru/freestream/demo"))
        self.assertFalse(urls.is_betboom_host("https://betboom.ru.evil.com/"))
        self.assertFalse(urls.is_betboom_host("javascript:alert(1)"))
        self.assertFalse(urls.is_betboom_host(""))


class FindUrlsTests(unittest.TestCase):
    def test_finds_urls_in_text_and_html_and_deduplicates_normalized_values(self):
        html = BeautifulSoup(
            """
            <div>
                Текст https://www.betboom.ru/freestream/demo#post.
                <a href="https://betboom.ru/freestream/demo#fragment">колесо</a>
                <a href="https://betboom.ru/freestream/other">второе</a>
            </div>
            """,
            "html.parser",
        )

        found = urls.find_urls(html, html.get_text(" ", strip=True))

        self.assertEqual(
            found,
            [
                "https://betboom.ru/freestream/demo",
                "https://betboom.ru/freestream/other",
            ],
        )

    def test_ignores_non_freestream_links_and_trailing_punctuation(self):
        html = BeautifulSoup(
            "<div>https://betboom.ru/other, https://example.com/freestream/a; "
            "https://betboom.ru/freestream/valid!</div>",
            "html.parser",
        )

        self.assertEqual(
            urls.find_urls(html, html.get_text(" ", strip=True)),
            ["https://betboom.ru/freestream/valid"],
        )

    def test_finds_schemeless_link_in_plain_text(self):
        # Twitch-чат — обычный текст, не HTML: <a href> там нет вовсе,
        # находка целиком зависит от регэкспа по тексту (см. FREESTREAM_RE).
        html = BeautifulSoup("<div>текст без ссылок</div>", "html.parser")

        self.assertEqual(
            urls.find_urls(html, "Го колесо betboom.ru/freestream/demo налетай"),
            ["https://betboom.ru/freestream/demo"],
        )

    def test_dedups_when_href_scheme_differs_from_bare_text_mention(self):
        # Реальный кейс @whylollybet: <a href="http://...">betboom.ru/...</a>
        # — href со схемой http, видимый текст ссылки без схемы вовсе.
        # Раньше это давало ДВЕ ссылки (http из href, https из текста) —
        # список urls поста менялся при каждом деплое, ложно засчитываясь
        # правкой поста (см. urls.normalize_url).
        html = BeautifulSoup(
            '<div><a href="http://betboom.ru/freestream/LOLLY08">'
            "betboom.ru/freestream/LOLLY08</a><br/>за полчасика до матча</div>",
            "html.parser",
        )
        text = html.get_text(" ", strip=True)

        self.assertEqual(
            urls.find_urls(html, text),
            ["https://betboom.ru/freestream/LOLLY08"],
        )

    def test_ignores_lookalike_host_without_scheme(self):
        # Без границы перед доменом опциональная схема заставила бы найти
        # «хвост» чужого домена и молча выдать его за настоящий betboom.ru.
        html = BeautifulSoup("<div>текст без ссылок</div>", "html.parser")

        self.assertEqual(
            urls.find_urls(html, "заходи на evilbetboom.ru/freestream/demo"),
            [],
        )


class FindDisallowedDomainsTests(unittest.TestCase):
    def test_allows_betboom_and_telegram_links(self):
        html = BeautifulSoup(
            '<div><a href="https://betboom.ru/freestream/demo">колесо</a> '
            '<a href="https://t.me/somechannel">канал</a></div>',
            "html.parser",
        )
        self.assertEqual(
            urls.find_disallowed_domains(html, html.get_text(" ", strip=True)), []
        )

    def test_allows_streamer_platform_links(self):
        # Обычный пост стримера: колесо плюс ссылки на его площадки
        # («Твич | ВК»). Такие домены не признак скама, и алерт по
        # ключевому слову из-за них дропаться не должен.
        html = BeautifulSoup(
            '<div>Колесо! '
            '<a href="https://www.twitch.tv/vlazhniy">твич</a> '
            '<a href="https://vk.com/vlazhniy">вк</a> '
            '<a href="https://youtu.be/abc">ютуб</a></div>',
            "html.parser",
        )
        self.assertEqual(
            urls.find_disallowed_domains(html, html.get_text(" ", strip=True)), []
        )

    def test_flags_third_party_casino_link(self):
        # Реальный кейс: скам-казино пишет «колесо на 60000$» и ведёт на
        # свой сайт вместо betboom.ru/freestream — такой пост не должен
        # уходить алертом наравне с настоящим колесом.
        html = BeautifulSoup(
            '<div>Колесо на 60000$ '
            '<a href="https://mellehdw.life/?open=register&p=qmf5">тут</a></div>',
            "html.parser",
        )
        self.assertEqual(
            urls.find_disallowed_domains(html, html.get_text(" ", strip=True)),
            ["mellehdw.life"],
        )

    def test_no_links_means_no_disallowed_domains(self):
        html = BeautifulSoup("<div>просто текст про колесо</div>", "html.parser")
        self.assertEqual(
            urls.find_disallowed_domains(html, html.get_text(" ", strip=True)), []
        )


def redirecting_session(location, head_status=301):
    """Сессия, отдающая редирект на HEAD (по умолчанию) или GET-фолбэк."""
    session = Mock()
    session.head.return_value = Mock(status_code=head_status, headers={"Location": location})
    return session


class ShortlinkCandidateTests(unittest.TestCase):
    def test_finds_known_shortener_in_text(self):
        self.assertEqual(
            urls.find_shortlink_candidates_in_text("го колесо vk.cc/aB3xZ налетай"),
            ["https://vk.cc/aB3xZ"],
        )

    def test_ignores_unknown_shortener(self):
        self.assertEqual(
            urls.find_shortlink_candidates_in_text("зайди на short.io/xyz"), []
        )

    def test_finds_candidate_in_href_and_text_and_dedups(self):
        html = BeautifulSoup(
            '<div><a href="https://vk.cc/aB3xZ">колесо</a> vk.cc/aB3xZ</div>',
            "html.parser",
        )
        self.assertEqual(
            urls.find_shortlink_candidates(html, html.get_text(" ", strip=True)),
            ["https://vk.cc/aB3xZ"],
        )

    def test_caps_candidates_to_max_shortlinks_per_message(self):
        many_links = " ".join(f"vk.cc/link{i}" for i in range(20))
        candidates = urls.find_shortlink_candidates_in_text(many_links)
        self.assertEqual(len(candidates), urls.MAX_SHORTLINKS_PER_MESSAGE)
        self.assertEqual(len(candidates), 5)

    def test_caps_candidates_in_node_to_max_shortlinks_per_message(self):
        links_html = "".join(f'<a href="https://vk.cc/link{i}">l{i}</a>' for i in range(20))
        html = BeautifulSoup(f"<div>{links_html}</div>", "html.parser").find("div")
        candidates = urls.find_shortlink_candidates(html, html.get_text(" ", strip=True))
        self.assertEqual(len(candidates), urls.MAX_SHORTLINKS_PER_MESSAGE)
        self.assertEqual(len(candidates), 5)


class ResolveShortlinkTests(unittest.TestCase):
    def setUp(self):
        urls._shortlink_cache.clear()
        self.addCleanup(urls._shortlink_cache.clear)

    def test_resolves_via_head_redirect(self):
        session = redirecting_session("https://betboom.ru/freestream/demo")

        resolved = urls.resolve_shortlink("https://vk.cc/aB3xZ", session)

        self.assertEqual(resolved, "https://betboom.ru/freestream/demo")

    def test_falls_back_to_get_when_head_is_not_a_redirect(self):
        # bit.ly и подобные нередко отвечают на HEAD 404/405, а Location
        # отдают только на GET.
        session = Mock()
        session.head.return_value = Mock(status_code=405, headers={})
        session.get.return_value = Mock(
            status_code=301, headers={"Location": "https://betboom.ru/freestream/demo"}
        )

        resolved = urls.resolve_shortlink("https://bit.ly/aB3xZ", session)

        self.assertEqual(resolved, "https://betboom.ru/freestream/demo")
        self.assertEqual(session.get.call_args.kwargs.get("allow_redirects"), False)

    def test_non_shortener_domain_is_not_resolved(self):
        session = redirecting_session("https://betboom.ru/freestream/demo")

        self.assertIsNone(urls.resolve_shortlink("https://example.com/abc", session))
        session.head.assert_not_called()

    def test_network_error_gives_none(self):
        session = Mock()
        session.head.side_effect = OSError("timeout")

        self.assertIsNone(urls.resolve_shortlink("https://vk.cc/aB3xZ", session))

    def test_chain_longer_than_max_hops_gives_none(self):
        # Сокращатель, ведущий на другой сокращатель, до бесконечности —
        # гоняться за такой цепочкой незачем (см. docstring resolve_shortlink).
        session = Mock()
        session.head.return_value = Mock(
            status_code=301, headers={"Location": "https://clck.ru/next"}
        )

        self.assertIsNone(
            urls.resolve_shortlink("https://vk.cc/aB3xZ", session, max_hops=2)
        )

    def test_chained_shorteners_resolve_within_hop_limit(self):
        session = Mock()
        session.head.side_effect = [
            Mock(status_code=301, headers={"Location": "https://clck.ru/next"}),
            Mock(
                status_code=301,
                headers={"Location": "https://betboom.ru/freestream/demo"},
            ),
        ]

        resolved = urls.resolve_shortlink("https://vk.cc/aB3xZ", session, max_hops=2)

        self.assertEqual(resolved, "https://betboom.ru/freestream/demo")

    def test_result_is_cached_and_second_call_skips_the_network(self):
        session = redirecting_session("https://betboom.ru/freestream/demo")

        first = urls.resolve_shortlink("https://vk.cc/aB3xZ", session)
        second = urls.resolve_shortlink("https://vk.cc/aB3xZ", session)

        self.assertEqual(first, second)
        session.head.assert_called_once()

    def test_failed_resolution_is_also_cached(self):
        session = Mock()
        session.head.side_effect = OSError("timeout")

        urls.resolve_shortlink("https://vk.cc/aB3xZ", session)
        urls.resolve_shortlink("https://vk.cc/aB3xZ", session)

        session.head.assert_called_once()

    def test_cache_expires_after_ttl(self):
        session = redirecting_session("https://betboom.ru/freestream/demo")
        base = datetime(2026, 1, 1, 12, 0, tzinfo=config.MSK_TZ)

        with patch("wheelsparser.urls.now_msk", return_value=base):
            urls.resolve_shortlink("https://vk.cc/aB3xZ", session)

        after_ttl = base + timedelta(
            seconds=config.SHORTENER_CACHE_TTL_SECONDS + 1
        )
        with patch("wheelsparser.urls.now_msk", return_value=after_ttl):
            urls.resolve_shortlink("https://vk.cc/aB3xZ", session)

        self.assertEqual(session.head.call_count, 2)


class FindUrlsShortlinkIntegrationTests(unittest.TestCase):
    def setUp(self):
        urls._shortlink_cache.clear()
        self.addCleanup(urls._shortlink_cache.clear)

    def test_resolved_shortlink_is_added_to_found_urls(self):
        html = BeautifulSoup(
            '<div>Колесо <a href="https://vk.cc/aB3xZ">тут</a></div>', "html.parser"
        )
        session = redirecting_session("https://betboom.ru/freestream/demo")

        found = urls.find_urls(html, html.get_text(" ", strip=True), session)

        self.assertEqual(found, ["https://betboom.ru/freestream/demo"])

    def test_without_session_shortlink_is_ignored(self):
        # Обратная совместимость: вызывающий без сети (тесты, старый код)
        # получает прежнее поведение — сокращатель просто не раскрывается.
        html = BeautifulSoup(
            '<div>Колесо <a href="https://vk.cc/aB3xZ">тут</a></div>', "html.parser"
        )

        self.assertEqual(urls.find_urls(html, html.get_text(" ", strip=True)), [])

    def test_shortlink_resolving_to_non_wheel_is_not_added(self):
        html = BeautifulSoup(
            '<div>Колесо <a href="https://vk.cc/aB3xZ">тут</a></div>', "html.parser"
        )
        session = redirecting_session("https://vk.com/somepost")

        self.assertEqual(
            urls.find_urls(html, html.get_text(" ", strip=True), session), []
        )


class FindDisallowedDomainsShortlinkIntegrationTests(unittest.TestCase):
    def setUp(self):
        urls._shortlink_cache.clear()
        self.addCleanup(urls._shortlink_cache.clear)

    def test_shortlink_resolving_to_allowed_domain_is_not_flagged(self):
        html = BeautifulSoup(
            '<div>Колесо <a href="https://vk.cc/aB3xZ">вк</a></div>', "html.parser"
        )
        session = redirecting_session("https://vk.com/somepost")

        self.assertEqual(
            urls.find_disallowed_domains(html, html.get_text(" ", strip=True), session),
            [],
        )

    def test_shortlink_resolving_to_scam_domain_is_flagged(self):
        html = BeautifulSoup(
            '<div>Колесо на 60000$ <a href="https://vk.cc/aB3xZ">тут</a></div>',
            "html.parser",
        )
        session = redirecting_session("https://mellehdw.life/?open=register")

        self.assertEqual(
            urls.find_disallowed_domains(html, html.get_text(" ", strip=True), session),
            ["mellehdw.life"],
        )

    def test_without_session_shortener_domain_itself_is_flagged(self):
        # Fail-secure по умолчанию: без раскрытия сокращатель — подозрительный
        # домен, а не заведомо разрешённый (см. docstring find_disallowed_domains).
        html = BeautifulSoup(
            '<div>Колесо <a href="https://vk.cc/aB3xZ">тут</a></div>', "html.parser"
        )

        self.assertEqual(
            urls.find_disallowed_domains(html, html.get_text(" ", strip=True)),
            ["vk.cc"],
        )

    def test_unresolvable_shortlink_keeps_shortener_domain_flagged(self):
        session = Mock()
        session.head.side_effect = OSError("timeout")
        html = BeautifulSoup(
            '<div>Колесо <a href="https://vk.cc/aB3xZ">тут</a></div>', "html.parser"
        )

        self.assertEqual(
            urls.find_disallowed_domains(html, html.get_text(" ", strip=True), session),
            ["vk.cc"],
        )


class ContentHashTests(unittest.TestCase):
    def test_hash_ignores_whitespace_changes(self):
        self.assertEqual(
            urls.message_content_hash("колесо   тут", []),
            urls.message_content_hash("колесо тут", []),
        )

    def test_hash_changes_when_href_changes_without_visible_text(self):
        text = "Новое колесо"
        self.assertNotEqual(
            urls.message_content_hash(text, ["https://betboom.ru/freestream/a"]),
            urls.message_content_hash(text, ["https://betboom.ru/freestream/b"]),
        )


if __name__ == "__main__":
    unittest.main()
