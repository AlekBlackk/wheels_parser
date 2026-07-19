# Task 1 Report: API status classification

## Delivered

- Added `_api_info_to_status(info: dict[str, Any]) -> str` beside the existing
  status-conversion helper in `betboom_web_parser.py`.
- Added focused unit coverage in `tests/test_active_api.py` for ended, early,
  running/joined, and incomplete API info objects.

## Classification behavior

| API info | Status |
| --- | --- |
| `is_ended` is `True` | `expired` |
| `is_ended` is `False`, `is_early` is `True` | `soon` |
| `is_ended` is `False`, `is_early` is `False` | `active` |
| Missing/non-boolean required state | `unknown` |

`is_joined` is deliberately ignored. An ended wheel is classified as
`expired` when `is_ended` is boolean `True`, even if `is_early` is absent;
otherwise an unended wheel needs a boolean `is_early` value.

## TDD evidence

1. Added the specified tests before adding production code.
2. Ran `python -m unittest tests.test_active_api.ApiStatusTests -v` and saw
   all four tests fail with `AttributeError` because `_api_info_to_status` did
   not yet exist.
3. After the behavior-priority clarification, implemented the minimal helper
   and reran the focused test command successfully.

## Verification

- `python -m unittest tests.test_active_api.ApiStatusTests -v` — 4 tests
  passed.
- `python -m unittest discover -v` — completed successfully; it discovered
  no tests in this repository because `tests` is not a discovery package.
- `git diff --check` — passed with no whitespace errors.

## Scope notes

The implementation is a pure status converter. It does not invoke Playwright,
does not add browser fallback behavior, and does not touch the existing
expired-cache handling.
