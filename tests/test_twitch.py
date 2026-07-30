import unittest
from unittest.mock import patch

from wheelsparser import alerts, twitch

WHEEL = "https://betboom.ru/freestream/stream1"


class IrcParsingTests(unittest.TestCase):
    def test_parses_tags_prefix_command_and_rest(self):
        line = (
            "@badges=broadcaster/1;mod=0 :user!user@user.tmi.twitch.tv "
            "PRIVMSG #demo :привет"
        )
        tags, prefix, command, rest = twitch.parse_irc_line(line)

        self.assertEqual(tags["badges"], "broadcaster/1")
        self.assertEqual(prefix, "user!user@user.tmi.twitch.tv")
        self.assertEqual(command, "PRIVMSG")
        self.assertEqual(rest, "#demo :привет")

    def test_parses_line_without_tags(self):
        tags, prefix, command, rest = twitch.parse_irc_line("PING :tmi.twitch.tv")
        self.assertEqual(tags, {})
        self.assertEqual(prefix, "")
        self.assertEqual(command, "PING")


class AuthorRoleTests(unittest.TestCase):
    def test_reads_roles_from_badges(self):
        roles = twitch.author_roles({"badges": "broadcaster/1,subscriber/12"}, "someone")
        self.assertEqual(roles, ["broadcaster"])

    def test_mod_tag_counts_as_moderator(self):
        self.assertEqual(twitch.author_roles({"mod": "1"}, "someone"), ["moderator"])

    def test_known_bot_login_is_trusted_without_badges(self):
        self.assertEqual(twitch.author_roles({}, "nightbot"), ["bot"])

    def test_plain_viewer_has_no_roles(self):
        self.assertEqual(twitch.author_roles({}, "viewer"), [])


class HandleMessageTests(unittest.TestCase):
    def _start(self, patcher):
        mock = patcher.start()
        self.addCleanup(patcher.stop)
        return mock

    def setUp(self):
        self._start(patch.dict(alerts.LAST_URL_ALERT, clear=True))
        self._start(patch.object(twitch, "precheck_wheel_status", return_value="active"))
        self.notify = self._start(
            patch.object(twitch, "send_telegram_notification", return_value=True)
        )
        while not twitch.TWITCH_NEW_ENTRIES.empty():
            twitch.TWITCH_NEW_ENTRIES.get_nowait()

    def queued(self):
        entries = []
        while not twitch.TWITCH_NEW_ENTRIES.empty():
            entries.append(twitch.TWITCH_NEW_ENTRIES.get_nowait())
        return entries

    def test_broadcaster_link_is_notified_and_queued(self):
        twitch.handle_twitch_message(
            "demo", "streamer", {"badges": "broadcaster/1"}, f"колесо {WHEEL}"
        )

        entries = self.queued()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["url"], WHEEL)
        self.assertEqual(entries[0]["source"], "twitch")
        self.assertEqual(entries[0]["author_roles"], ["broadcaster"])
        self.notify.assert_called_once()

    def test_viewer_link_is_ignored(self):
        twitch.handle_twitch_message("demo", "viewer", {}, f"колесо {WHEEL}")

        self.assertEqual(self.queued(), [])
        self.notify.assert_not_called()

    def test_expired_wheel_is_not_notified_but_starts_cooldown(self):
        with patch.object(twitch, "precheck_wheel_status", return_value="expired"):
            twitch.handle_twitch_message(
                "demo", "streamer", {"badges": "broadcaster/1"}, WHEEL
            )

        self.assertEqual(self.queued(), [])
        self.notify.assert_not_called()
        self.assertIsNotNone(alerts.last_alert(WHEEL))

    def test_repeated_link_within_cooldown_is_sent_once(self):
        for _ in range(2):
            twitch.handle_twitch_message(
                "demo", "nightbot", {}, f"розыгрыш {WHEEL}"
            )

        self.assertEqual(len(self.queued()), 1)
        self.notify.assert_called_once()

    def test_message_without_wheel_link_is_skipped(self):
        twitch.handle_twitch_message(
            "demo", "streamer", {"badges": "broadcaster/1"}, "просто колесо"
        )
        self.assertEqual(self.queued(), [])
        self.notify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
