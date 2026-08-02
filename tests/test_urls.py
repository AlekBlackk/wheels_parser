import unittest

from bs4 import BeautifulSoup

from wheelsparser import urls


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
