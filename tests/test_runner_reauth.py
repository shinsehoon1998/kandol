import unittest
from types import SimpleNamespace

from solting_auto import runner
from solting_auto.dedup import PhoneDedup
from solting_auto.kb_reauth import ReauthRequired
from solting_auto.excel_reader import Record
from solting_auto.reporter import SUCCESS


class _Logger:
    def __init__(self):
        self.messages = []

    def info(self, message):
        self.messages.append(("info", message))

    def warning(self, message):
        self.messages.append(("warning", message))

    def error(self, message):
        self.messages.append(("error", message))


class _FakeInsurance:
    def __init__(self):
        self.ins = {"stamping_enabled": False}
        self.required = False
        self.wait_calls = 0
        self.register_calls = 0

    def detect_reauth(self):
        return self.required

    def wait_for_reauth(self, row_no=None):
        self.wait_calls += 1
        self.required = False
        return "resumed"

    def register_and_consent(self, jumin, name, phone):
        self.register_calls += 1
        if self.register_calls == 1:
            raise ReauthRequired("인증방법 선택 창 감지")
        return ""


class RunnerReauthTest(unittest.TestCase):
    def test_reauth_retries_same_row_without_failure_or_consecutive_failure(self):
        engine = _FakeInsurance()
        dedup = PhoneDedup()
        rec = Record(row_no=2, jumin="9001011234567", name="테스트고객", phone="01012345678")
        logger = _Logger()

        result = runner._process_row_until_reauth(
            rec,
            [runner.STAGE_INSURANCE],
            {runner.STAGE_INSURANCE: engine},
            {runner.STAGE_INSURANCE: dedup},
            {"retry_count": 0, "retry_delay_sec": 0},
            False,
            "/tmp/kandol-test-shots",
            logger,
            False,
        )

        self.assertEqual(result.status, SUCCESS)
        self.assertEqual(engine.register_calls, 2)
        self.assertEqual(engine.wait_calls, 1)
        self.assertIn("01012345678", dedup._registered)
        self.assertFalse(any("실패" in msg for _, msg in logger.messages))


if __name__ == "__main__":
    unittest.main()
