"""Поиск ключевых слов в тексте поста с учётом русской морфологии."""

from __future__ import annotations

import functools
import re

from . import registry

# Частые русские окончания для поиска по границам слова: «колесо» найдёт
# «колеса», «колесом», «колёсами». Порядок не влияет на корректность
# (regex перебирает альтернативы с backtracking), но длинные идут первыми.
_RU_ENDINGS = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими",
    "ая", "яя", "ое", "ее", "ые", "ие", "ой", "ей", "ом", "ем",
    "ам", "ям", "ах", "ях", "ов", "ев", "ым", "им", "ых", "их",
    "ую", "юю", "ий", "ый",
    "а", "я", "о", "е", "у", "ю", "ы", "и", "й", "ь",
)
_WORD_CHARS = "0-9A-Za-zА-Яа-яЁё_"


def normalize_for_match(text: str) -> str:
    """casefold + «ё» → «е», чтобы «колёса» совпадало с «колеса»."""
    return text.casefold().replace("ё", "е")


@functools.lru_cache(maxsize=256)
def keyword_regex(keyword: str) -> re.Pattern[str]:
    """Компилирует регэксп для одного ключевого слова.

    - «*слово*» — поиск по подстроке (старое поведение: найдёт «суперколесо»);
    - «слово» — по границам слова с учётом русских окончаний:
      «колесо» найдёт «колесо», «колеса», «колесом», «колёсами»,
      но не «колесовать» и не «околесица».
    Для фраз («фрибет колесо») окончания допускаются у каждого слова.
    """
    raw = normalize_for_match(keyword.strip())
    if raw.startswith("*") and raw.endswith("*") and len(raw) > 2:
        return re.compile(re.escape(raw.strip("*")))
    endings = "|".join(_RU_ENDINGS)
    token_patterns: list[str] = []
    for token in raw.split():
        stem = token
        # Окончание самого ключевого слова тоже отбрасываем:
        # «колесо» → основа «колес» + любое окончание из списка.
        for ending in _RU_ENDINGS:
            if stem.endswith(ending) and len(stem) - len(ending) >= 3:
                stem = stem[: len(stem) - len(ending)]
                break
        token_patterns.append(rf"{re.escape(stem)}(?:{endings})?")
    body = r"\s+".join(token_patterns)
    return re.compile(rf"(?<![{_WORD_CHARS}]){body}(?![{_WORD_CHARS}])")


def find_keywords(text: str) -> list[str]:
    """Ключевые слова, найденные в тексте сообщения.

    Поиск регистронезависимый, «ё» и «е» считаются одной буквой.
    «слово» ищется по границам слова с учётом окончаний,
    «*слово*» — по подстроке (см. keyword_regex).
    """
    if not text:
        return []
    normalized = normalize_for_match(text)
    return [
        keyword
        for keyword in registry.keywords_snapshot()
        if keyword_regex(keyword).search(normalized)
    ]


# Регэксп для проверки контекста BetBoom / фрибета в тексте поста.
# Ищет упоминания BetBoom (betboom, бетбум/бэтбум с окончаниями), фрибета (фрибет, freebet),
# фристрима (фристрим, freestream) и сокращений (бб/ббшка, bb/bbшка).
_BETBOOM_CONTEXT_RE = re.compile(
    r"(?<![0-9a-zа-я_])(?:"
    r"bet[-\s]?boom|б[еэ]т[-\s]?бум(?:[а-я]+)?"
    r"|free[-\s]?bets?|фри[-\s]?бет(?:[а-я]+)?"
    r"|free[-\s]?streams?|фри[-\s]?стрим(?:[а-я]+)?"
    r"|бб(?:[а-я]+)?|bb(?:[а-я]+)?"
    r")(?![0-9a-zа-я_])"
)


def has_betboom_context(text: str) -> bool:
    """Проверяет наличие контекста BetBoom / фрибета в тексте сообщения.

    Ищет упоминания: betboom, бетбум, бб, bb, фрибет, фристрим, freestream.
    Используется для фильтрации ложных срабатываний по широким ключевым
    словам (например, «колесо» в отрыве от ссылок BetBoom).
    """
    if not text:
        return False
    normalized = normalize_for_match(text)
    return bool(_BETBOOM_CONTEXT_RE.search(normalized))
