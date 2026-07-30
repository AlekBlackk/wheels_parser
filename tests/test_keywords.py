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


if __name__ == "__main__":
    unittest.main()
