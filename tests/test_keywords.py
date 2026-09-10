import unittest
from unittest.mock import patch

from wheelsparser import keywords, registry


class KeywordMatchingTests(unittest.TestCase):
    def find(self, text, words=("колесо",)):
        with patch.object(registry, "KEYWORDS", list(words)):
            return keywords.find_keywords(text)

    def test_matches_regardless_of_case_and_yo(self):
        self.assertEqual(self.find("Сегодня КОЛЁСА будут"), ["колесо"])

    def test_matches_russian_endings(self):
        for text in ("колесо", "колеса", "колесом", "колёсами", "колесу"):
            self.assertEqual(self.find(f"будет {text} вечером"), ["колесо"], text)

    def test_does_not_match_unrelated_words_with_same_stem(self):
        for text in ("колесовать", "околесица", "колесник"):
            self.assertEqual(self.find(f"это {text}"), [], text)

    def test_substring_form_matches_inside_word(self):
        self.assertEqual(self.find("суперколесо", words=("*колесо*",)), ["*колесо*"])

    def test_plain_form_does_not_match_inside_word(self):
        self.assertEqual(self.find("суперколесо"), [])

    def test_phrase_allows_endings_in_every_word(self):
        self.assertEqual(
            self.find("раздаём фрибеты колёсами", words=("фрибет колесо",)),
            ["фрибет колесо"],
        )

    def test_phrase_does_not_match_derived_words(self):
        # Список окончаний покрывает склонение, а не словообразование:
        # «фрибетные» — другое слово, а не форма «фрибета».
        self.assertEqual(
            self.find("раздаём фрибетные колёса", words=("фрибет колесо",)), []
        )

    def test_empty_text_matches_nothing(self):
        self.assertEqual(self.find(""), [])


class BetboomContextTests(unittest.TestCase):
    def test_positive_betboom_mentions(self):
        cases = (
            "будет betboom колесо",
            "Сегодня BetBoom запустил раздачу",
            "Заходите в BETBOOM",
            "Ссылка на bet-boom",
            "Раздача в bet boom",
            "колесо в бетбум",
            "выиграл в бетбуме фрибет",
            "новости бетбума",
            "на бет-бум колесо",
            "в бет бум раздача",
            "колесо в бэтбум",
            "выиграл в бэтбуме фрибет",
            "новости бэтбума",
            "на бэт-бум колесо",
            "в бэт бум раздача",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(keywords.has_betboom_context(text))

    def test_positive_freebet_mentions(self):
        cases = (
            "раздаём фрибет за колесо",
            "получи фрибеты",
            "много фрибетов",
            "бонус фрибетом",
            "забирайте фрибетами",
            "freebet wheel",
            "get freebets",
            "free-bet promo",
            "free bet bonus",
            "раздача фри бет",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(keywords.has_betboom_context(text))

    def test_positive_freestream_mentions(self):
        cases = (
            "запущен фристрим",
            "в фристриме новое колесо",
            "freestream wheel",
            "free-stream link",
            "free stream online",
            "фри стрим на канале",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(keywords.has_betboom_context(text))

    def test_positive_abbreviation_mentions(self):
        cases = (
            "раздача на бб",
            "колесо на bb",
            "крутим (бб)",
            "раздача «бб»",
            "#бб колесо",
            "заходи на бб!",
            "колесо на ббшке",
            "раздача на bbшке",
            "колесо на ббхе",
            "выиграл на ббшку",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(keywords.has_betboom_context(text))

    def test_negative_unrelated_texts(self):
        cases = (
            "поменял колесо на машине",
            "новое колесо удачи в игре",
            "крутите колесо на welvura.com",
            "бобёр построил плотину",
            "я люблю эту песню",
            "идём на bbq сегодня",
            "смотрим bbc news",
            "версия subb обновлена",
            "новое хобби",
            "суббота выходной",
            "просто текст без ключевых слов",
            "",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertFalse(keywords.has_betboom_context(text))


if __name__ == "__main__":
    unittest.main()
