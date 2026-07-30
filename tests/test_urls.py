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
