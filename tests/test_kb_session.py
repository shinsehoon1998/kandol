import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from solting_auto import kb_session as ks


def _page(url, typ="page"):
    return {"type": typ, "url": url, "title": ""}


class ClassifyTabsTest(unittest.TestCase):
    def test_logged_in_when_kb_main_tab_exists(self):
        tabs = [_page("https://nsales.kbinsure.co.kr/eus/main/main.jsp")]
        self.assertEqual(ks.classify_tabs(tabs)[0], ks.STATE_LOGGED_IN)

    def test_login_page_only(self):
        tabs = [_page("https://nsales.kbinsure.co.kr/eus/ch/ch_index.jsp")]
        self.assertEqual(ks.classify_tabs(tabs)[0], ks.STATE_LOGIN_PAGE)

    def test_login_root_with_port(self):
        tabs = [_page("https://nsales.kbinsure.co.kr:8500/")]
        self.assertEqual(ks.classify_tabs(tabs)[0], ks.STATE_LOGIN_PAGE)

    def test_no_kb_tab(self):
        tabs = [_page("https://www.naver.com/"), _page("about:blank")]
        self.assertEqual(ks.classify_tabs(tabs)[0], ks.STATE_NO_KB_TAB)

    def test_non_page_targets_ignored(self):
        tabs = [_page("https://nsales.kbinsure.co.kr/main", typ="service_worker")]
        self.assertEqual(ks.classify_tabs(tabs)[0], ks.STATE_NO_KB_TAB)

    def test_lookalike_host_is_not_kb(self):
        tabs = [_page("https://evil-kbinsure.co.kr.example.com/main")]
        self.assertEqual(ks.classify_tabs(tabs)[0], ks.STATE_NO_KB_TAB)
        tabs = [_page("https://example.com/?next=https://nsales.kbinsure.co.kr/main")]
        self.assertEqual(ks.classify_tabs(tabs)[0], ks.STATE_NO_KB_TAB)

    def test_main_tab_wins_over_login_tab(self):
        tabs = [_page("https://nsales.kbinsure.co.kr/eus/ch/ch_index.jsp"),
                _page("https://nsales.kbinsure.co.kr/wsserver/main.do")]
        state, url = ks.classify_tabs(tabs)
        self.assertEqual(state, ks.STATE_LOGGED_IN)
        self.assertIn("wsserver", url)

    def test_empty(self):
        self.assertEqual(ks.classify_tabs([])[0], ks.STATE_NO_KB_TAB)


class _Handler(BaseHTTPRequestHandler):
    payload = []

    def do_GET(self):
        if self.path != "/json/list":
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(type(self).payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class ProbeTest(unittest.TestCase):
    def _serve(self, payload):
        _Handler.payload = payload
        srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self.addCleanup(srv.server_close)
        return f"http://127.0.0.1:{srv.server_address[1]}"

    def test_probe_logged_in(self):
        url = self._serve([_page("https://nsales.kbinsure.co.kr/eus/main/main.jsp")])
        self.assertEqual(ks.probe_kb_session(url)[0], ks.STATE_LOGGED_IN)

    def test_probe_login_page(self):
        url = self._serve([_page("https://nsales.kbinsure.co.kr/eus/ch/ch_index.jsp")])
        self.assertEqual(ks.probe_kb_session(url)[0], ks.STATE_LOGIN_PAGE)

    def test_probe_no_browser(self):
        # 아무도 듣지 않는 포트
        self.assertEqual(ks.probe_kb_session("http://127.0.0.1:1", timeout=0.5)[0], ks.STATE_NO_BROWSER)

    def test_probe_garbage_response(self):
        url = self._serve({"not": "a list"})
        self.assertEqual(ks.probe_kb_session(url)[0], ks.STATE_NO_BROWSER)


if __name__ == "__main__":
    unittest.main()
