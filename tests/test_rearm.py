import json
import unittest
from datetime import timedelta
from unittest.mock import Mock, patch

from tests.dbfixture import use_temp_db
from wheelsparser import db, rearm
from wheelsparser.timeutils import now_msk


class WatchTargetsTests(unittest.TestCase):
    """73% колёс — перезапуск адреса, который уже есть в истории. Порядок
    обхода решает, кого мы успеем проверить в пределах бюджета."""

    def setUp(self):
        use_temp_db(self)

    def _seen(self, slug: str, days_ago: float) -> None:
        db.insert_entries([{
            "url": f"https://betboom.ru/freestream/{slug}",
            "found_at": (now_msk() - timedelta(days=days_ago)).isoformat(
                timespec="seconds"
            ),
            "channel": "demo",
            "source": "telegram",
        }])

    def test_address_seen_on_more_days_ranks_higher(self):
        self._seen("rare", 1)
        for day in (1, 2, 3):
            self._seen("hot", day)

        targets = rearm.rank_known_addresses()

        self.assertEqual(targets[0], "https://betboom.ru/freestream/hot")

    def test_ties_are_broken_by_recency(self):
        self._seen("stale", 9)
        self._seen("fresh", 1)

        targets = rearm.rank_known_addresses()

        self.assertEqual(targets[0], "https://betboom.ru/freestream/fresh")

    def test_addresses_outside_the_window_are_dropped(self):
        self._seen("ancient", 400)
        self._seen("recent", 1)

        self.assertEqual(
            rearm.rank_known_addresses(),
            ["https://betboom.ru/freestream/recent"],
        )

    def test_repeat_on_the_same_day_counts_once(self):
        # Иначе одно болтливое колесо с десятком повторов за вечер
        # вытеснило бы из бюджета всех остальных.
        for _ in range(5):
            self._seen("chatty", 1)
        for day in (1, 2):
            self._seen("steady", day)

        targets = rearm.rank_known_addresses()

        self.assertEqual(targets[0], "https://betboom.ru/freestream/steady")

    def test_series_base_is_watched_even_if_never_seen(self):
        # zonertg существует и переиспользуется, но в истории только
        # zonertg3…13 — голый адрес не найдёт никто, кроме наблюдателя.
        self._seen("zonertg7", 1)
        frontier = {"zonertg": {"index": 7, "width": 1, "pending": []}}

        targets = rearm.watch_targets(frontier, limit=10)

        self.assertIn("https://betboom.ru/freestream/zonertg", targets)

    def test_limit_truncates_the_tail_not_the_head(self):
        for day in (1, 2, 3):
            self._seen("hot", day)
        self._seen("cold", 5)

        targets = rearm.watch_targets({}, limit=1)

        self.assertEqual(targets, ["https://betboom.ru/freestream/hot"])


def page(status_code, uid=None):
    payload = json.dumps({"props": {"pageProps": {"uid": uid, "hash": "jwt"}}})
    body = "" if uid is None else (
        f'<script id="__NEXT_DATA__" type="application/json">{payload}</script>'
    )
    return Mock(status_code=status_code, text=body)


class ProbeActionUidTests(unittest.TestCase):
    """Дешёвый детектор: одна страница вместо страницы плюс API."""

    def test_reads_uid_from_next_data(self):
        session = Mock()
        session.get.return_value = page(200, "uid-42")

        self.assertEqual(
            rearm.probe_action_uid("https://betboom.ru/freestream/over", session),
            (rearm.FOUND, "uid-42"),
        )

    def test_missing_page_reports_missing(self):
        session = Mock()
        session.get.return_value = page(404)

        outcome, _uid = rearm.probe_action_uid("https://x/gone", session)

        self.assertEqual(outcome, rearm.MISSING)

    def test_throttling_reports_blocked(self):
        for code in (403, 429):
            session = Mock()
            session.get.return_value = page(code)

            outcome, _uid = rearm.probe_action_uid("https://x/one", session)

            self.assertEqual(outcome, rearm.BLOCKED)

    def test_page_without_next_data_reports_error(self):
        session = Mock()
        session.get.return_value = page(200)

        outcome, _uid = rearm.probe_action_uid("https://x/one", session)

        self.assertEqual(outcome, rearm.ERROR)

    def test_network_failure_reports_error(self):
        session = Mock()
        session.get.side_effect = rearm.requests.Timeout("slow")

        outcome, _uid = rearm.probe_action_uid("https://x/one", session)

        self.assertEqual(outcome, rearm.ERROR)


class WatchAddressTests(unittest.TestCase):
    def setUp(self):
        use_temp_db(self)

    def test_same_uid_costs_one_request_and_notifies_nothing(self):
        session = Mock()
        session.get.return_value = page(200, "uid-1")

        with patch.object(rearm, "precheck_wheel") as precheck, \
             patch.object(rearm, "notify_rearmed_wheel") as notify:
            outcome, uid = rearm.watch_address("https://x/over", "uid-1", session)

        self.assertEqual((outcome, uid), (rearm.FOUND, "uid-1"))
        precheck.assert_not_called()
        notify.assert_not_called()

    def test_new_uid_that_is_active_is_notified(self):
        session = Mock()
        session.get.return_value = page(200, "uid-2")

        with patch.object(
            rearm, "precheck_wheel", return_value=("active", False, "до 21:40")
        ), patch.object(rearm, "notify_rearmed_wheel") as notify:
            outcome, uid = rearm.watch_address("https://x/over", "uid-1", session)

        self.assertEqual((outcome, uid), (rearm.FOUND, "uid-2"))
        notify.assert_called_once()

    def test_new_uid_that_is_not_active_is_remembered_but_silent(self):
        # Колесо создано, но ещё не запущено: uid уже новый, а рассылать
        # нечего. Запоминаем uid, иначе старт мы потом не отличим.
        session = Mock()
        session.get.return_value = page(200, "uid-2")

        with patch.object(rearm, "precheck_wheel", return_value=("soon", False, "")), \
             patch.object(rearm, "notify_rearmed_wheel") as notify:
            outcome, uid = rearm.watch_address("https://x/over", "uid-1", session)

        self.assertEqual((outcome, uid), (rearm.FOUND, "uid-2"))
        notify.assert_not_called()

    def test_unknown_address_is_learned_without_notifying(self):
        # Первая встреча с адресом: сравнивать не с чем, и считать это
        # перезапуском нельзя — иначе первый же проход разошлёт всё подряд.
        session = Mock()
        session.get.return_value = page(200, "uid-1")

        with patch.object(rearm, "precheck_wheel") as precheck, \
             patch.object(rearm, "notify_rearmed_wheel") as notify:
            outcome, uid = rearm.watch_address("https://x/over", None, session)

        self.assertEqual((outcome, uid), (rearm.FOUND, "uid-1"))
        precheck.assert_not_called()
        notify.assert_not_called()


class PassAllowanceTests(unittest.TestCase):
    """Бюджет распределяется по времени суток. Прежний сканер тратил его
    подряд и замолкал в 14:02 — ровно перед вечерним пиком."""

    def _at(self, hour: int) -> int:
        return rearm.pass_allowance(3000, 900, now_msk().replace(hour=hour))

    def test_peak_hour_gets_more_than_daytime(self):
        self.assertGreater(self._at(19), self._at(11))

    def test_daytime_gets_more_than_night(self):
        self.assertGreater(self._at(11), self._at(4))

    def test_night_still_gets_at_least_one_request(self):
        # Ночью колёса редки, но не невозможны: 03:00 не должно означать
        # полную слепоту, иначе ночной запуск не увидит никто.
        self.assertGreaterEqual(self._at(4), 1)

    def test_allowance_scales_with_the_daily_budget(self):
        small = rearm.pass_allowance(600, 900, now_msk().replace(hour=19))
        large = rearm.pass_allowance(3000, 900, now_msk().replace(hour=19))

        self.assertGreater(large, small)

    def test_a_full_day_of_passes_stays_within_the_daily_budget(self):
        interval = 900
        passes_per_hour = 3600 // interval
        spent = sum(
            rearm.pass_allowance(3000, interval, now_msk().replace(hour=hour))
            * passes_per_hour
            for hour in range(24)
        )

        self.assertLessEqual(spent, 3000)


class FakeBudget:
    def __init__(self, limit):
        self.limit = limit
        self.used = 0

    def take(self):
        if self.used >= self.limit:
            return False
        self.used += 1
        return True


class WatchOnceTests(unittest.TestCase):
    def setUp(self):
        use_temp_db(self)
        rearm.reset_cursor()
        self.addCleanup(rearm.reset_cursor)
        self.targets = [f"https://betboom.ru/freestream/w{n}" for n in range(6)]
        patcher = patch.object(rearm, "watch_targets", return_value=self.targets)
        patcher.start()
        self.addCleanup(patcher.stop)
        frontier = patch.object(rearm, "load_streamer_frontier", return_value={})
        frontier.start()
        self.addCleanup(frontier.stop)

    def _run(self, allowance, uids, watch):
        with patch.object(rearm, "load_watched_uids", return_value=dict(uids)), \
             patch.object(rearm, "save_watched_uids") as save, \
             patch.object(rearm, "watch_address", side_effect=watch) as probe:
            allowed = rearm.watch_once(FakeBudget(100), Mock(), allowance)
        return allowed, probe, save

    def test_probes_only_up_to_the_allowance(self):
        _allowed, probe, _save = self._run(
            2, {}, lambda url, uid, session: (rearm.FOUND, "uid")
        )

        self.assertEqual(probe.call_count, 2)

    def test_next_pass_continues_where_the_previous_stopped(self):
        watch = lambda url, uid, session: (rearm.FOUND, "uid")  # noqa: E731
        self._run(2, {}, watch)

        _allowed, probe, _save = self._run(2, {}, watch)

        probed = [call.args[0] for call in probe.call_args_list]
        self.assertEqual(probed, self.targets[2:4])

    def test_blocked_stops_the_pass_immediately(self):
        allowed, probe, _save = self._run(
            5, {}, lambda url, uid, session: (rearm.BLOCKED, "")
        )

        self.assertFalse(allowed)
        self.assertEqual(probe.call_count, 1)

    def test_learned_uids_are_saved(self):
        _allowed, _probe, save = self._run(
            1, {}, lambda url, uid, session: (rearm.FOUND, "uid-new")
        )

        saved = save.call_args.args[0]
        self.assertEqual(saved[self.targets[0]], "uid-new")

    def test_addresses_that_left_the_pool_are_forgotten(self):
        # Файл не должен копить адреса, выпавшие из окна истории.
        stale = {"https://betboom.ru/freestream/ancient": "uid-old"}

        _allowed, _probe, save = self._run(
            1, stale, lambda url, uid, session: (rearm.FOUND, "uid")
        )

        self.assertNotIn("https://betboom.ru/freestream/ancient", save.call_args.args[0])

    def test_exhausted_budget_stops_the_pass_without_blocking(self):
        with patch.object(rearm, "load_watched_uids", return_value={}), \
             patch.object(rearm, "save_watched_uids"), \
             patch.object(
                 rearm, "watch_address",
                 side_effect=lambda url, uid, session: (rearm.FOUND, "uid"),
             ) as probe:
            allowed = rearm.watch_once(FakeBudget(2), Mock(), allowance=5)

        self.assertTrue(allowed)
        self.assertEqual(probe.call_count, 2)


if __name__ == "__main__":
    unittest.main()
