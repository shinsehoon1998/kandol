import unittest

from solting_auto.insurance import InsuranceAutomation
from solting_auto.kb_reauth import AUTH_REQUIRED, SESSION_EXPIRED, STATE_CLEAR


class _Page:
    def __init__(self, url, title=""):
        self.url = url
        self._title = title

    def title(self):
        return self._title


class _Context:
    def __init__(self, pages):
        self.pages = pages


class _Browser:
    def __init__(self, contexts):
        self.contexts = contexts


class _Logger:
    def info(self, message):
        pass

    def warning(self, message):
        pass


class InsuranceReauthProbeTest(unittest.TestCase):
    def _automation(self, contexts):
        auto = object.__new__(InsuranceAutomation)
        auto._browser = _Browser(contexts)
        auto.context = None
        auto.ins = {"reauth": {}}
        auto.log = _Logger()
        auto._last_login_monotonic = None
        auto._pre_warning_sent = False
        return auto

    def test_scans_all_context_pages_for_auth_window(self):
        auto = self._automation([
            _Context([_Page("https://nsales.kbinsure.co.kr/eus/wsserver/main.do")]),
            _Context([_Page("https://nsales.kbinsure.co.kr/eus/ch/member/ch_mobile2track.jsp", "인증방법 선택")]),
        ])
        state = auto.detect_reauth()
        self.assertEqual(state.state, AUTH_REQUIRED)
        self.assertTrue(state.reauth_window)

    def test_entrance_without_auth_window_is_expired(self):
        auto = self._automation([
            _Context([_Page("https://nsales.kbinsure.co.kr/eus/ch/entrance/ch_index_ok.jstl")]),
        ])
        state = auto.detect_reauth()
        self.assertEqual(state.state, SESSION_EXPIRED)
        self.assertTrue(state.session_expired)

    def test_logged_in_page_is_clear(self):
        auto = self._automation([
            _Context([_Page("https://nsales.kbinsure.co.kr/eus/wsserver/main.do")]),
        ])
        state = auto.detect_reauth()
        self.assertEqual(state.state, STATE_CLEAR)
        self.assertTrue(state.logged_in)


if __name__ == "__main__":
    unittest.main()
