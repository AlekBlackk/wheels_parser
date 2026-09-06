import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.dbfixture import use_temp_db
from wheelsparser import alerts, betboom, db, predictive, storage


def page(status_code):
    return Mock(status_code=status_code)


class SplitSlugTests(unittest.TestCase):
    """Ширина числового хвоста и регистр — не косметика: LOLLY8 вместо
    LOLLY08 и lolly08 вместо LOLLY08 одинаково отдают 404."""

    def test_splits_unpadded_series(self):
        self.assertEqual(predictive.split_slug("zonertg4"), ("zonertg", 4, 1))

    def test_splits_zero_padded_series_and_keeps_width(self):
        self.assertEqual(predictive.split_slug("LOLLY08"), ("LOLLY", 8, 2))

    def test_keeps_original_case(self):
        prefix, _index, _width = predictive.split_slug("LOLLY12")
        self.assertEqual(prefix, "LOLLY")

    def test_slug_without_number_is_not_a_series(self):
        self.assertIsNone(predictive.split_slug("aunkereref"))
        self.assertIsNone(predictive.split_slug("same"))

    def test_slug_of_only_digits_is_not_a_series(self):
        # Префикс обязан быть непустым: иначе «1234» дало бы серию без имени.
        self.assertIsNone(predictive.split_slug("1234"))


class BuildSlugTests(unittest.TestCase):
    def test_restores_zero_padding(self):
        self.assertEqual(predictive.build_slug("LOLLY", 9, 2), "LOLLY09")

    def test_keeps_number_longer_than_padding(self):
        # Ширина — минимум, а не обрезка: 13 при ширине 2 остаётся «13».
        self.assertEqual(predictive.build_slug("LOLLY", 13, 2), "LOLLY13")

    def test_unpadded_series_grows_into_two_digits(self):
        self.assertEqual(predictive.build_slug("zonertg", 10, 1), "zonertg10")

    def test_round_trip_through_split(self):
        for slug in ("zonertg4", "LOLLY08", "LOLLY13", "zonertg10"):
            prefix, index, width = predictive.split_slug(slug)
            self.assertEqual(predictive.build_slug(prefix, index, width), slug)


class FrontierFromHistoryTests(unittest.TestCase):
    """Серии не задаются руками: рубеж берётся из того, что уже нашли
    Telegram и Twitch, — новый стример подхватывается сам."""

    def setUp(self):
        use_temp_db(self)

    def _store(self, *slugs):
        db.insert_entries([
            {
                "url": f"https://betboom.ru/freestream/{slug}",
                "found_at": predictive.now_msk().isoformat(timespec="seconds"),
                "channel": "demo",
                "source": "telegram",
            }
            for slug in slugs
        ])

    def test_takes_highest_index_per_series(self):
        self._store("zonertg4", "zonertg7", "zonertg5")

        frontier = predictive.frontier_from_history()

        self.assertEqual(frontier["zonertg"]["index"], 7)
        self.assertEqual(frontier["zonertg"]["width"], 1)

    def test_tracks_series_separately_and_keeps_case(self):
        self._store("zonertg7", "LOLLY12")

        frontier = predictive.frontier_from_history()

        self.assertEqual(sorted(frontier), ["LOLLY", "zonertg"])
        self.assertEqual(frontier["LOLLY"]["width"], 2)

    def test_ignores_slugs_without_a_number(self):
        self._store("aunkereref")

        self.assertEqual(predictive.frontier_from_history(), {})

    def test_validates_prefix_regex(self):
        # 1-буквенный префикс или недопустимые символы отбрасываются
        self._store(
            "a1",
            "bad!slug2",
            "ok_prefix-12",
            "verylongprefixthatexceedsthirtytwocharacters1",
        )

        frontier = predictive.frontier_from_history()

        self.assertIn("ok_prefix-", frontier)
        self.assertNotIn("a", frontier)
        self.assertNotIn("bad!slug", frontier)
        self.assertNotIn("verylongprefixthatexceedsthirtytwocharacters", frontier)


class MergeFrontiersTests(unittest.TestCase):
    def setUp(self):
        predictive._RETIRED_SERIES.clear()
        self.addCleanup(predictive._RETIRED_SERIES.clear)

    def test_takes_the_larger_index(self):
        stored = {"zonertg": {"index": 9, "width": 1, "pending": []}}
        history = {"zonertg": {"index": 7, "width": 1, "pending": []}}

        merged = predictive.merge_frontiers(stored, history)

        # Файл помнит адреса, которых нет в базе: завершившиеся колёса,
        # найденные перебором, в историю не пишутся.
        self.assertEqual(merged["zonertg"]["index"], 9)

    def test_history_wins_when_it_is_ahead(self):
        stored = {"zonertg": {"index": 4, "width": 1, "pending": []}}
        history = {"zonertg": {"index": 7, "width": 1, "pending": []}}

        self.assertEqual(predictive.merge_frontiers(stored, history)["zonertg"]["index"], 7)

    def test_series_from_either_source_are_kept(self):
        merged = predictive.merge_frontiers(
            {"a": {"index": 1, "width": 1, "pending": []}},
            {"b": {"index": 2, "width": 1, "pending": []}},
        )
        self.assertEqual(sorted(merged), ["a", "b"])

    def test_retired_series_is_ignored_if_history_not_advanced(self):
        predictive._RETIRED_SERIES["zonertg"] = 10
        stored = {}
        history = {"zonertg": {"index": 10, "width": 1, "pending": []}}
        merged = predictive.merge_frontiers(stored, history)
        self.assertNotIn("zonertg", merged)

    def test_retired_series_is_reinstated_if_history_advances(self):
        predictive._RETIRED_SERIES["zonertg"] = 10
        stored = {}
        history = {"zonertg": {"index": 11, "width": 1, "pending": []}}
        merged = predictive.merge_frontiers(stored, history)
        self.assertIn("zonertg", merged)
        self.assertEqual(merged["zonertg"]["index"], 11)
        self.assertNotIn("zonertg", predictive._RETIRED_SERIES)


class BudgetTests(unittest.TestCase):
    def test_allows_up_to_the_limit(self):
        budget = predictive.Budget(2)
        self.assertTrue(budget.take())
        self.assertTrue(budget.take())
        self.assertFalse(budget.take())

    def test_resets_on_a_new_day(self):
        budget = predictive.Budget(1)
        self.assertTrue(budget.take())
        self.assertFalse(budget.take())

        with patch.object(predictive, "today_msk", return_value="2099-01-01"):
            self.assertTrue(budget.take())


class ProbeSlugTests(unittest.TestCase):
    def test_missing_page_does_not_reach_the_api(self):
        session = Mock()
        session.get.return_value = page(404)

        with patch.object(predictive, "precheck_wheel") as precheck:
            outcome, *_ = predictive.probe_slug("zonertg99", session)

        self.assertEqual(outcome, predictive.MISSING)
        precheck.assert_not_called()

    def test_throttling_response_reports_blocked(self):
        for code in (403, 429):
            session = Mock()
            session.get.return_value = page(code)
            with patch.object(predictive, "precheck_wheel") as precheck:
                outcome, *_ = predictive.probe_slug("zonertg9", session)
            self.assertEqual(outcome, predictive.BLOCKED)
            precheck.assert_not_called()

    def test_request_exception_with_429_response_reports_blocked(self):
        # Если requests бросает RequestException с прикреплённым 429/403,
        # сканер всё равно обязан определить BLOCKED.
        for code in (403, 429):
            session = Mock()
            err = predictive.requests.RequestException("rate limited")
            err.response = page(code)
            session.get.side_effect = err
            outcome, *_ = predictive.probe_slug("zonertg9", session)
            self.assertEqual(outcome, predictive.BLOCKED)

    def test_predictive_session_excludes_429_from_retries(self):
        # 429 не должен ретраиться urllib3, иначе сканер долбит сервер
        # 5 запросами на слаг и падает в RetryError вместо BLOCKED.
        predictive.PREDICTIVE_SESSION = None
        session = predictive._session()
        adapter = session.adapters["https://"]
        self.assertNotIn(429, adapter.max_retries.status_forcelist)

    def test_network_error_reports_error(self):
        session = Mock()
        session.get.side_effect = predictive.requests.Timeout("slow")

        outcome, *_ = predictive.probe_slug("zonertg9", session)

        self.assertEqual(outcome, predictive.ERROR)

    def test_existing_page_is_checked_through_the_api(self):
        session = Mock()
        session.get.return_value = page(200)

        with patch.object(
            predictive, "precheck_wheel", return_value=("active", True, "срок")
        ) as precheck:
            outcome, status, referral, ends_at = predictive.probe_slug("zonertg10", session)

        self.assertEqual((outcome, status, referral, ends_at), (
            predictive.FOUND, "active", True, "срок"
        ))
        # Кэш expired обходится (перебор — честная проверка), а счётчик
        # заглушки не трогается: серия expired подряд для сканера — норма.
        self.assertEqual(precheck.call_args.kwargs["use_cache"], False)
        self.assertEqual(precheck.call_args.kwargs["feed_stub_guard"], False)


class NotifyFoundWheelTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.dict(alerts.LAST_URL_ALERT, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        while not predictive.PREDICTIVE_NEW_ENTRIES.empty():
            predictive.PREDICTIVE_NEW_ENTRIES.get_nowait()
        self.addCleanup(self._drain)

    def _drain(self):
        while not predictive.PREDICTIVE_NEW_ENTRIES.empty():
            predictive.PREDICTIVE_NEW_ENTRIES.get_nowait()

    def queued(self):
        entries = []
        while not predictive.PREDICTIVE_NEW_ENTRIES.empty():
            entries.append(predictive.PREDICTIVE_NEW_ENTRIES.get_nowait())
        return entries

    def test_sends_notification_and_queues_entry(self):
        with patch.object(
            predictive, "send_telegram_notification", return_value=True
        ) as send:
            predictive.notify_found_wheel("zonertg", "zonertg10", "active", False, "")

        send.assert_called_once()
        (entry,) = self.queued()
        self.assertEqual(entry["url"], "https://betboom.ru/freestream/zonertg10")
        self.assertEqual(entry["source"], "predictive")
        self.assertEqual(entry["channel"], "zonertg")
        self.assertTrue(entry["notified"])

    def test_cooldown_suppresses_a_duplicate_of_another_source(self):
        # То же колесо мог уже прислать пост в Telegram или чат Twitch —
        # кулдаун в alerts общий для всех источников.
        url = "https://betboom.ru/freestream/zonertg10"
        alerts.mark_url_alert(url, predictive.now_msk())

        with patch.object(predictive, "send_telegram_notification") as send:
            predictive.notify_found_wheel("zonertg", "zonertg10", "active", False, "")

        send.assert_not_called()
        self.assertEqual(self.queued(), [])

    def test_entry_is_queued_even_when_sending_crashes(self):
        # Кулдаун уже поставлен, и без записи находка пропала бы совсем.
        with patch.object(
            predictive, "send_telegram_notification", side_effect=RuntimeError("boom")
        ):
            predictive.notify_found_wheel("zonertg", "zonertg10", "active", False, "")

        (entry,) = self.queued()
        self.assertFalse(entry["notified"])

    def test_cooldown_claimed_by_scanner_blocks_subsequent_claims(self):
        with patch.object(
            predictive, "send_telegram_notification", return_value=True
        ) as send:
            predictive.notify_found_wheel("zonertg", "zonertg10", "active", False, "")

        send.assert_called_once()
        with patch.object(predictive, "send_telegram_notification") as send2:
            predictive.notify_found_wheel("zonertg", "zonertg10", "active", False, "")
        send2.assert_not_called()


class ScanSeriesTests(unittest.TestCase):
    def setUp(self):
        self._start(patch.object(predictive, "_pause_between_requests"))
        self.notify = self._start(patch.object(predictive, "notify_found_wheel"))
        self.session = Mock()

    def _start(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def _series(self, index=7, width=1, pending=None):
        return {"index": index, "width": width, "pending": list(pending or [])}

    def test_stops_at_the_first_missing_address(self):
        # Реальные серии сплошные, поэтому 404 — конец серии, а не пропуск:
        # продолжать перебор незачем, это лишние запросы к чужому сайту.
        with patch.object(
            predictive, "probe_slug", return_value=(predictive.MISSING, "", False, "")
        ) as probe:
            updated, allowed = predictive.scan_series(
                "zonertg", self._series(), predictive.Budget(10), self.session
            )

        self.assertTrue(allowed)
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(updated["index"], 7)

    def test_advances_past_missed_expired_wheels(self):
        # Пропущенное завершившееся колесо не повод для уведомления, но
        # рубеж обязан сдвинуться — иначе сканер упрётся в него навсегда.
        outcomes = [
            (predictive.FOUND, "expired", False, ""),
            (predictive.FOUND, "expired", False, ""),
            (predictive.MISSING, "", False, ""),
        ]
        with patch.object(predictive, "probe_slug", side_effect=outcomes):
            updated, allowed = predictive.scan_series(
                "zonertg", self._series(), predictive.Budget(10), self.session
            )

        self.assertTrue(allowed)
        self.assertEqual(updated["index"], 9)
        self.notify.assert_not_called()

    def test_notifies_about_a_live_wheel(self):
        outcomes = [
            (predictive.FOUND, "active", False, "срок"),
            (predictive.MISSING, "", False, ""),
        ]
        with patch.object(predictive, "probe_slug", side_effect=outcomes):
            updated, _allowed = predictive.scan_series(
                "zonertg", self._series(), predictive.Budget(10), self.session
            )

        self.notify.assert_called_once_with("zonertg", "zonertg8", "active", False, "срок")
        self.assertEqual(updated["index"], 8)

    def test_created_but_unstarted_wheel_is_remembered_not_announced(self):
        outcomes = [
            (predictive.FOUND, "soon", False, ""),
            (predictive.MISSING, "", False, ""),
        ]
        with patch.object(predictive, "probe_slug", side_effect=outcomes):
            updated, _allowed = predictive.scan_series(
                "zonertg", self._series(), predictive.Budget(10), self.session
            )

        self.notify.assert_not_called()
        self.assertEqual(updated, {"index": 8, "width": 1, "pending": [8]})

    def test_soon_frontier_is_rechecked_and_announced_when_it_starts(self):
        # Главный сценарий: колесо создано, но не запущено — ждём старта
        # и уведомляем в тот же цикл, когда оно стало active.
        outcomes = [
            (predictive.FOUND, "active", False, "срок"),  # перепроверка рубежа
            (predictive.MISSING, "", False, ""),          # следующий адрес
        ]
        with patch.object(predictive, "probe_slug", side_effect=outcomes) as probe:
            updated, _allowed = predictive.scan_series(
                "zonertg", self._series(pending=[7]), predictive.Budget(10), self.session
            )

        self.assertEqual(probe.call_args_list[0].args[0], "zonertg7")
        self.notify.assert_called_once_with("zonertg", "zonertg7", "active", False, "срок")
        self.assertEqual(updated["pending"], [])

    def test_several_waiting_wheels_in_one_series_are_all_remembered(self):
        # Регресс с живого прогона: у серии zonertw подряд оказались два
        # созданных, но не запущенных колеса. Пока хранился статус только
        # последнего адреса, старт первого проходил бы незамеченным.
        outcomes = [
            (predictive.FOUND, "soon", False, ""),
            (predictive.FOUND, "soon", False, ""),
            (predictive.MISSING, "", False, ""),
        ]
        with patch.object(predictive, "probe_slug", side_effect=outcomes):
            updated, _allowed = predictive.scan_series(
                "zonertw", self._series(), predictive.Budget(10), self.session
            )

        self.assertEqual(updated["pending"], [8, 9])

    def test_every_waiting_wheel_is_rechecked(self):
        outcomes = [
            (predictive.FOUND, "soon", False, ""),    # 8 всё ещё ждёт
            (predictive.FOUND, "active", False, ""),  # 9 стартовало
            (predictive.MISSING, "", False, ""),      # 10 не существует
        ]
        with patch.object(predictive, "probe_slug", side_effect=outcomes) as probe:
            updated, _allowed = predictive.scan_series(
                "zonertw", self._series(index=9, pending=[8, 9]),
                predictive.Budget(10), self.session,
            )

        self.assertEqual(
            [call.args[0] for call in probe.call_args_list],
            ["zonertw8", "zonertw9", "zonertw10"],
        )
        self.notify.assert_called_once_with("zonertw", "zonertw9", "active", False, "")
        # Стартовавшее ушло из ожидания, ещё не начавшееся осталось.
        self.assertEqual(updated["pending"], [8])

    def test_waiting_wheel_that_ended_is_dropped_without_a_notification(self):
        outcomes = [
            (predictive.FOUND, "expired", False, ""),
            (predictive.MISSING, "", False, ""),
        ]
        with patch.object(predictive, "probe_slug", side_effect=outcomes):
            updated, _allowed = predictive.scan_series(
                "zonertw", self._series(pending=[7]), predictive.Budget(10), self.session
            )

        self.notify.assert_not_called()
        self.assertEqual(updated["pending"], [])

    def test_waiting_list_is_capped(self):
        # Колесо, созданное и заброшенное навсегда, иначе вечно съедало бы
        # по запросу за цикл.
        outcomes = [(predictive.FOUND, "soon", False, "")] * 10
        with patch.object(predictive, "probe_slug", side_effect=outcomes), \
             patch.object(predictive, "PREDICTIVE_LOOKAHEAD", 8):
            updated, _allowed = predictive.scan_series(
                "zonertw", self._series(), predictive.Budget(20), self.session
            )

        self.assertEqual(len(updated["pending"]), predictive.MAX_PENDING_PER_SERIES)
        # Оставляем свежие — у них больше шансов запуститься.
        self.assertEqual(updated["pending"], [11, 12, 13, 14, 15])

    def test_lookahead_limits_the_number_of_probes(self):
        with patch.object(
            predictive, "probe_slug",
            return_value=(predictive.FOUND, "expired", False, ""),
        ) as probe, patch.object(predictive, "PREDICTIVE_LOOKAHEAD", 3):
            predictive.scan_series(
                "zonertg", self._series(), predictive.Budget(100), self.session
            )

        self.assertEqual(probe.call_count, 3)

    def test_exhausted_budget_stops_the_scan(self):
        with patch.object(
            predictive, "probe_slug",
            return_value=(predictive.FOUND, "expired", False, ""),
        ) as probe:
            predictive.scan_series(
                "zonertg", self._series(), predictive.Budget(2), self.session
            )

        self.assertEqual(probe.call_count, 2)

    def test_throttling_stops_everything_immediately(self):
        with patch.object(
            predictive, "probe_slug",
            return_value=(predictive.BLOCKED, "", False, ""),
        ) as probe:
            _updated, allowed = predictive.scan_series(
                "zonertg", self._series(), predictive.Budget(10), self.session
            )

        self.assertFalse(allowed)
        self.assertEqual(probe.call_count, 1)

    def test_increments_empty_scans_on_unfruitful_scan(self):
        with patch.object(
            predictive, "probe_slug",
            return_value=(predictive.MISSING, "", False, ""),
        ):
            updated, _ = predictive.scan_series(
                "zonertg", self._series(), predictive.Budget(10), self.session
            )
        self.assertEqual(updated.get("empty_scans"), 1)

    def test_resets_empty_scans_when_wheel_found(self):
        outcomes = [
            (predictive.FOUND, "active", False, "срок"),
            (predictive.MISSING, "", False, ""),
        ]
        with patch.object(predictive, "probe_slug", side_effect=outcomes):
            updated, _ = predictive.scan_series(
                "zonertg", {"index": 7, "width": 1, "pending": [], "empty_scans": 3},
                predictive.Budget(10), self.session
            )
        self.assertNotIn("empty_scans", updated)


class ScanOnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="wheelsparser-predictive-"))
        self._start(patch.object(storage, "STREAMERS_FILE", self.tmp / "streamers.json"))
        self._start(
            patch.object(storage, "RETIRED_STREAMERS_FILE", self.tmp / "retired_streamers.json")
        )
        self._start(patch.object(predictive, "_pause_between_requests"))
        self._start(patch.object(predictive, "notify_found_wheel"))
        predictive._RETIRED_SERIES.clear()
        self.addCleanup(predictive._RETIRED_SERIES.clear)
        self.session = Mock()

    def _start(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def test_frontier_is_saved_between_runs(self):
        history = {"zonertg": {"index": 7, "width": 1, "pending": []}}
        outcomes = [
            (predictive.FOUND, "expired", False, ""),
            (predictive.MISSING, "", False, ""),
        ]
        with patch.object(predictive, "frontier_from_history", return_value=history), \
             patch.object(predictive, "probe_slug", side_effect=outcomes):
            predictive.scan_once(predictive.Budget(10), self.session)

        self.assertEqual(storage.load_streamer_frontier()["zonertg"]["index"], 8)

    def test_no_series_is_a_cheap_noop(self):
        with patch.object(predictive, "frontier_from_history", return_value={}), \
             patch.object(predictive, "probe_slug") as probe:
            self.assertTrue(predictive.scan_once(predictive.Budget(10), self.session))

        probe.assert_not_called()

    def test_throttling_reports_that_scanning_must_stop(self):
        history = {"zonertg": {"index": 7, "width": 1, "pending": []}}
        with patch.object(predictive, "frontier_from_history", return_value=history), \
             patch.object(
                 predictive, "probe_slug",
                 return_value=(predictive.BLOCKED, "", False, ""),
             ):
            self.assertFalse(predictive.scan_once(predictive.Budget(10), self.session))

    def test_series_exceeding_max_empty_scans_is_retired(self):
        history = {
            "deadseries": {
                "index": 5,
                "width": 1,
                "pending": [],
                "empty_scans": predictive.PREDICTIVE_MAX_EMPTY_SCANS - 1,
            }
        }
        with patch.object(predictive, "frontier_from_history", return_value=history), \
             patch.object(
                 predictive, "probe_slug",
                 return_value=(predictive.MISSING, "", False, ""),
             ):
            predictive.scan_once(predictive.Budget(10), self.session)

        frontier = storage.load_streamer_frontier()
        self.assertNotIn("deadseries", frontier)
        self.assertEqual(predictive._RETIRED_SERIES.get("deadseries"), 5)
        # Сохранилось на диск в retired_streamers.json
        self.assertEqual(storage.load_retired_series().get("deadseries"), 5)

    def test_empty_scans_accumulates_across_multiple_scans(self):
        history = {"zonertg": {"index": 7, "width": 1, "pending": []}}
        missing_probe = (predictive.MISSING, "", False, "")
        found_probe = (predictive.FOUND, "expired", False, "")
        with patch.object(predictive, "frontier_from_history", return_value=history), \
             patch.object(predictive, "probe_slug", return_value=missing_probe):
            # 1-й проход: пустой скан
            predictive.scan_once(predictive.Budget(10), self.session)
            f1 = storage.load_streamer_frontier()
            self.assertEqual(f1["zonertg"]["empty_scans"], 1)

            # 2-й проход: снова пустой скан -> счётчик увеличивается
            predictive.scan_once(predictive.Budget(10), self.session)
            f2 = storage.load_streamer_frontier()
            self.assertEqual(f2["zonertg"]["empty_scans"], 2)

        # 3-й проход: найдено колесо -> счётчик сбрасывается
        with patch.object(predictive, "frontier_from_history", return_value={}), \
             patch.object(predictive, "probe_slug", return_value=found_probe):
            predictive.scan_once(predictive.Budget(10), self.session)
            f3 = storage.load_streamer_frontier()
            self.assertNotIn("empty_scans", f3["zonertg"])

    def test_blocked_does_not_increment_empty_scans(self):
        history = {"zonertg": {"index": 7, "width": 1, "pending": []}}
        blocked_probe = (predictive.BLOCKED, "", False, "")
        with patch.object(predictive, "frontier_from_history", return_value=history), \
             patch.object(predictive, "probe_slug", return_value=blocked_probe):
            predictive.scan_once(predictive.Budget(10), self.session)
            frontier = storage.load_streamer_frontier()
            self.assertNotIn("empty_scans", frontier["zonertg"])


class StubGuardIsolationTests(unittest.TestCase):
    """Сканер намеренно ходит по старым адресам серии, и серия expired
    подряд для него — норма. Без исключения из счётчика он сам сваливал бы
    парсер в fail-open на первом же проходе по пропущенным колёсам."""

    def setUp(self):
        betboom._signature_cache.clear()
        betboom._expired_cache.clear()
        betboom._consecutive_expired = 0
        betboom._stub_guard_active = False
        self.addCleanup(betboom._signature_cache.clear)
        self.addCleanup(betboom._expired_cache.clear)
        self.addCleanup(setattr, betboom, "_consecutive_expired", 0)
        self.addCleanup(setattr, betboom, "_stub_guard_active", False)

    def test_scanner_probes_do_not_advance_the_stub_guard(self):
        from tests.test_betboom import ended_info, wheel_session

        session = wheel_session(
            Mock(status_code=200, json=Mock(return_value={"info": ended_info()}))
        )
        for index in range(betboom.BETBOOM_STUB_GUARD_THRESHOLD * 2):
            status, *_ = betboom.precheck_wheel(
                f"https://betboom.ru/freestream/probe{index}",
                session,
                use_cache=False,
                feed_stub_guard=False,
            )
            self.assertEqual(status, "expired")

        self.assertEqual(betboom._consecutive_expired, 0)
        self.assertFalse(betboom._stub_guard_active)


if __name__ == "__main__":
    unittest.main()
