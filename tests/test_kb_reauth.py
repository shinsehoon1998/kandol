import datetime as dt
import unittest

from solting_auto.kb_reauth import (
    AUTH_REQUIRED,
    SESSION_EXPIRED,
    WAIT_RESUMED,
    WAIT_STOPPED,
    WAIT_TIMEOUT,
    detect_reauth_state,
    is_reauth_page,
    is_session_expired_page,
    wait_for_reauth,
)


def page(url, title=""):
    return {"url": url, "title": title}


class ReauthDetectionTest(unittest.TestCase):
    def test_normal_kb_page_is_clear(self):
        state = detect_reauth_state([page("https://nsales.kbinsure.co.kr/eus/wsserver/main.do")])
        self.assertFalse(state.reauth_window)
        self.assertFalse(state.session_expired)

    def test_auth_window_by_path_and_title(self):
        self.assertTrue(is_reauth_page(
            "https://nsales.kbinsure.co.kr/eus/ch/member/ch_mobile2track.jsp", ""
        ))
        self.assertTrue(is_reauth_page(
            "https://nsales.kbinsure.co.kr/eus/ch/member/other.jsp", "인증방법 선택"
        ))
        state = detect_reauth_state([
            page("https://nsales.kbinsure.co.kr/eus/ch/member/ch_mobile2track.jsp", "인증방법 선택")
        ])
        self.assertEqual(state.state, AUTH_REQUIRED)
        self.assertTrue(state.reauth_window)

    def test_entrance_is_expired(self):
        url = "https://nsales.kbinsure.co.kr/eus/ch/entrance/ch_index_ok.jstl"
        self.assertTrue(is_session_expired_page(url))
        self.assertEqual(detect_reauth_state([page(url)]).state, SESSION_EXPIRED)

    def test_similar_domain_and_query_string_are_not_trusted(self):
        self.assertFalse(is_reauth_page(
            "https://kbinsure.co.kr.evil.com/eus/ch/member/ch_mobile2track.jsp", "인증방법 선택"
        ))
        self.assertFalse(is_session_expired_page(
            "https://example.com/?next=https://nsales.kbinsure.co.kr/eus/ch/entrance/"
        ))
        state = detect_reauth_state([
            page("https://kbinsure.co.kr.evil.com/eus/ch/member/ch_mobile2track.jsp", "인증방법 선택"),
            page("https://example.com/?q=kbinsure.co.kr/eus/ch/entrance/", ""),
        ])
        self.assertFalse(state.reauth_window)
        self.assertFalse(state.session_expired)


class FakeClock:
    def __init__(self, start):
        self.value = float(start)
        self.wall = dt.datetime(2026, 10, 26, 12, 0, 0)

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds
        self.wall += dt.timedelta(seconds=seconds)

    def now(self):
        return self.wall


class ReauthWaitTest(unittest.TestCase):
    def test_immediate_alert_escalation_and_thirty_minute_reminder(self):
        clock = FakeClock(0)
        notifications = []
        probes = iter([False] * 3 + [True, True])

        result = wait_for_reauth(
            probe=lambda: next(probes),
            notify=lambda kind, **kwargs: notifications.append((kind, kwargs)),
            stop_requested=lambda: False,
            clock=clock,
            now=clock.now,
            sleep=clock.sleep,
            poll_interval_sec=300,
            resume_interval_sec=300,
            escalation_after_sec=600,
            reminder_interval_sec=1800,
            max_wait_sec=3600,
        )

        self.assertEqual(result, WAIT_RESUMED)
        self.assertEqual(notifications[0][0], "auth_required")
        self.assertTrue(any(k == "reminder" and v.get("escalate_sms") for k, v in notifications))

    def test_quiet_hours_suppress_reminders_and_send_one_at_seven(self):
        clock = FakeClock(0)
        clock.wall = dt.datetime(2026, 10, 26, 23, 30, 0)
        notifications = []
        probes = iter([False] * 15 + [True, True])

        result = wait_for_reauth(
            probe=lambda: next(probes),
            notify=lambda kind, **kwargs: notifications.append((kind, kwargs)),
            stop_requested=lambda: False,
            clock=clock,
            now=clock.now,
            sleep=clock.sleep,
            poll_interval_sec=1800,
            resume_interval_sec=1800,
            escalation_after_sec=600,
            reminder_interval_sec=1800,
            max_wait_sec=10 * 3600,
            quiet_start="23:00",
            quiet_end="07:00",
        )

        self.assertEqual(result, WAIT_RESUMED)
        reminders = [item for item in notifications if item[0] == "reminder"]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0][1]["waited_min"], 450)

    def test_two_consecutive_clear_probes_are_required(self):
        clock = FakeClock(0)
        notifications = []
        states = iter([True, False, True, True])
        result = wait_for_reauth(
            probe=lambda: next(states),
            notify=lambda kind, **kwargs: notifications.append(kind),
            stop_requested=lambda: False,
            clock=clock,
            sleep=clock.sleep,
            poll_interval_sec=1,
            resume_interval_sec=5,
            max_wait_sec=60,
        )
        self.assertEqual(result, WAIT_RESUMED)
        self.assertEqual(notifications[-1], "resumed")
        self.assertEqual(clock.value, 11)

    def test_stop_and_timeout(self):
        clock = FakeClock(0)
        self.assertEqual(
            wait_for_reauth(
                probe=lambda: False,
                notify=lambda *args, **kwargs: None,
                stop_requested=lambda: True,
                clock=clock,
                sleep=clock.sleep,
                max_wait_sec=10,
            ),
            WAIT_STOPPED,
        )
        clock = FakeClock(0)
        self.assertEqual(
            wait_for_reauth(
                probe=lambda: False,
                notify=lambda *args, **kwargs: None,
                stop_requested=lambda: False,
                clock=clock,
                sleep=clock.sleep,
                poll_interval_sec=5,
                max_wait_sec=10,
            ),
            WAIT_TIMEOUT,
        )


if __name__ == "__main__":
    unittest.main()
