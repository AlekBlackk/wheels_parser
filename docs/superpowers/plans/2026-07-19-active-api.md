# Active API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/active` determine wheel status through BetBoom's JSON API without opening Playwright or Chromium.

**Architecture:** Normalize each freestream URL to its path without tracking parameters and POST it to BetBoom's `get-info` endpoint. Convert `info.is_ended` and `info.is_early` to the existing `expired`, `soon`, and `active` states, retain today's expired cache, and return `unknown` for failures.

**Tech Stack:** Python 3.10+, `requests`, `concurrent.futures.ThreadPoolExecutor`, `unittest`.

## Global Constraints

- `/active` must not invoke Playwright or a browser.
- API failures must be counted as `unknown`; no browser fallback is allowed.
- `info.is_joined` must not affect the wheel's status.
- Preserve the existing result format and daily MSK expired-cache semantics.

---

## File Structure

- `betboom_web_parser.py` — API endpoint constants, response-to-status conversion, concurrent `/active` check, and fire-and-forget delivery using the API worker.
- `tests/test_active_api.py` — isolated unit tests for status conversion and API request handling with a fake session.
- `requirements.txt` — remove Playwright because it becomes unused.

### Task 1: Define and test API status conversion

**Files:**
- Modify: `betboom_web_parser.py:700-750`
- Create: `tests/test_active_api.py`

**Interfaces:**
- Produces: `_api_info_to_status(info: dict[str, Any]) -> str`
- Consumes: API `info` object containing booleans `is_ended` and `is_early`.

- [ ] **Step 1: Write the failing test**

```python
import unittest
import betboom_web_parser as parser


class ApiStatusTests(unittest.TestCase):
    def test_marks_ended_wheel_expired(self):
        self.assertEqual(parser._api_info_to_status({"is_ended": True}), "expired")

    def test_marks_early_wheel_soon(self):
        self.assertEqual(
            parser._api_info_to_status({"is_ended": False, "is_early": True}),
            "soon",
        )

    def test_marks_running_wheel_active_regardless_of_join_state(self):
        self.assertEqual(
            parser._api_info_to_status(
                {"is_ended": False, "is_early": False, "is_joined": True}
            ),
            "active",
        )

    def test_rejects_incomplete_info(self):
        self.assertEqual(parser._api_info_to_status({"is_ended": False}), "unknown")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_active_api.ApiStatusTests -v`

Expected: failure because `_api_info_to_status` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
def _api_info_to_status(info: dict[str, Any]) -> str:
    is_ended = info.get("is_ended")
    is_early = info.get("is_early")
    if not isinstance(is_ended, bool) or not isinstance(is_early, bool):
        return "unknown"
    if is_ended:
        return "expired"
    return "soon" if is_early else "active"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_active_api.ApiStatusTests -v`

Expected: all four tests pass.

- [ ] **Step 5: Commit**

```bash
git add betboom_web_parser.py tests/test_active_api.py
git commit -m "feat: classify wheel API states"
```

### Task 2: Implement direct API request and concurrent active check

**Files:**
- Modify: `betboom_web_parser.py:20, 190-210, 750-1090`
- Modify: `tests/test_active_api.py`

**Interfaces:**
- Produces: `_check_wheel_api(item: dict[str, Any], session: requests.Session) -> str`
- Produces: `_get_active_api(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]`
- Consumes: `STREAMER_WHEEL_INFO_API`, `HEADERS`, `REQUEST_TIMEOUT`, `ACTIVE_CHECK_CONCURRENCY`, `_api_info_to_status`.

- [ ] **Step 1: Write the failing test**

```python
from unittest.mock import Mock

def test_api_check_posts_normalized_freestream_url(self):
    response = Mock()
    response.status_code = 200
    response.json.return_value = {
        "code": 200,
        "status": "OK",
        "info": {"is_ended": False, "is_early": False},
    }
    session = Mock()
    session.post.return_value = response

    status = parser._check_wheel_api(
        {"url": "https://betboom.ru/freestream/zonertg10?utm_source=test"},
        session,
    )

    self.assertEqual(status, "active")
    self.assertEqual(
        session.post.call_args.kwargs["json"],
        {"streamer_link": "https://betboom.ru/freestream/zonertg10"},
    )

def test_api_check_returns_unknown_for_http_failure(self):
    response = Mock(status_code=503)
    session = Mock()
    session.post.return_value = response
    self.assertEqual(
        parser._check_wheel_api({"url": "https://betboom.ru/freestream/a"}, session),
        "unknown",
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_active_api -v`

Expected: failure because `_check_wheel_api` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
STREAMER_WHEEL_INFO_API = "https://betboom.ru/api/streamer-wheel/action/get-info"

def _freestream_url(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

def _check_wheel_api(item: dict[str, Any], session: requests.Session) -> str:
    url = _freestream_url(str(item.get("url", "")))
    if not url:
        return "unknown"
    response = session.post(
        STREAMER_WHEEL_INFO_API,
        json={"streamer_link": url},
        headers={**HEADERS, "Accept": "application/json", "X-Platform": "web", "Referer": url},
        timeout=REQUEST_TIMEOUT,
    )
    if response.status_code != 200:
        return "unknown"
    payload = response.json()
    return _api_info_to_status(payload.get("info", {}))
```

Implement `_get_active_api` using `ThreadPoolExecutor(max_workers=ACTIVE_CHECK_CONCURRENCY)`: skip same-day expired URLs, preserve input order, cache `expired`, count `unknown`, and return the active/soon items plus unknown count. Replace `_fire_active_check`'s Playwright coroutine with a daemon worker thread that calls `_get_active_api`, formats the result, sends it through `_pw_bot_send` renamed to `_background_bot_send`, and releases the single-check lock in `finally`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m unittest tests.test_active_api -v`

Expected: all status and request tests pass.

- [ ] **Step 5: Commit**

```bash
git add betboom_web_parser.py tests/test_active_api.py
git commit -m "feat: check active wheels through BetBoom API"
```

### Task 3: Remove browser-only active-check dependencies and verify live API

**Files:**
- Modify: `betboom_web_parser.py:20-40, 700-1090`
- Modify: `requirements.txt`

**Interfaces:**
- Keeps: `_fire_active_check(chat_id, unique_items)` public command entrypoint.
- Removes: Playwright-specific `/active` helpers and `playwright` dependency.

- [ ] **Step 1: Write the failing regression test**

```python
def test_active_check_module_does_not_require_playwright(self):
    self.assertFalse(hasattr(parser, "async_playwright"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m unittest tests.test_active_api -v`

Expected: failure while the Playwright import remains.

- [ ] **Step 3: Write minimal implementation**

Remove the `playwright.async_api` import block, all `_pw_*` browser lifecycle and DOM-check functions, and the `playwright` line from `requirements.txt`. Update error strings in `_format_active_result` from renderer/browser-specific wording to API-check wording.

- [ ] **Step 4: Run automated and live verification**

Run: `python -m unittest discover -s tests -v`

Expected: all tests pass.

Run:

```powershell
@'
import betboom_web_parser as parser
import requests
print(parser._check_wheel_api({"url": "https://betboom.ru/freestream/zonertg10?utm_content=zoner&utm_source=freestream"}, requests.Session()))
'@ | python -
```

Expected: `active`, with no Chromium process started.

- [ ] **Step 5: Commit**

```bash
git add betboom_web_parser.py requirements.txt tests/test_active_api.py
git commit -m "refactor: remove Playwright active checker"
```
