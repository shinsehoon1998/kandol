"""2026-10-27 추가 — '사망자 정보 확인에 따른 고객정보 처리 제한 안내' 팝업 감지 테스트.

대표가 실제 KB 화면을 촬영해 보내준 문구를 그대로 양성(positive) 사례로 쓴다. 지금까지
_detect_server_error/_detect_name_error 같은 모달 감지 함수는 이 저장소에 테스트가 하나도
없었다 — 최소한의 가짜 Playwright locator로 실문구 매칭만 검증한다(브라우저 미사용).
"""
import unittest

from solting_auto.insurance import InsuranceAutomation


# 2026-10-26 캡처(대표 사진) 원문. 실제 KB 모달에서 긁힌 inner_text 그대로(개행 포함).
REAL_DEATH_NOTICE_TEXT = """알림
사망자 정보 확인에 따른 고객정보 처리 제한 안내
· 해당 고객에 대하여 신용정보원으로부터 사망자 정보가 제공되었습니다.
· 사망자 명의의 부정한 금융거래 방지를 위해 동의 업무가 제한됩니다.
· 해당 고객 관련 업무는 반드시 업무별 제한 기준과 예외처리 절차를 확인하여 진행하여 주시기 바랍니다.
다만, 오등록으로 생존 고객이 확인되는 경우
· 본인확인 및 생존 사실 확인 서류를 확인한 후 승인 절차에 따라 당사에 한하여 업무 제한을 해제할 수 있습니다.
· 이와 관련된 문의는 데이터지원파트로 문의하여 주시기 바랍니다.
· 고객에게 한국신용정보원을 통한 정정 신청 절차를 안내하여 주시기 바랍니다.
· 한국신용정보원 소비자보호부: 1544-1040
확인"""


class _FakeElement:
    def __init__(self, text, visible=True):
        self._text = text
        self._visible = visible
        self.clicked = False

    def is_visible(self):
        return self._visible

    def inner_text(self):
        return self._text

    def click(self, **kwargs):
        self.clicked = True

    def evaluate(self, *_a, **_kw):
        self.clicked = True

    def locator(self, _sel):
        return _FakeLocator([])  # 모달 내부 버튼 탐색은 비워둠(클릭 시도만 안 터지면 됨)


class _FakeLocator:
    def __init__(self, elements):
        self._elements = elements

    def count(self):
        return len(self._elements)

    def nth(self, i):
        return self._elements[i]


class _FakeFrame:
    """selector -> 매칭 엘리먼트 목록 만 흉내 낸다. 버튼 클릭용 selector는 전부 빈 목록."""

    def __init__(self, selector_matches):
        self._selector_matches = dict(selector_matches)

    def locator(self, sel):
        return _FakeLocator(self._selector_matches.get(sel, []))


class _Logger:
    def info(self, *_a, **_kw):
        pass

    def warning(self, *_a, **_kw):
        pass

    def debug(self, *_a, **_kw):
        pass


def _automation():
    auto = object.__new__(InsuranceAutomation)
    auto.page = None  # _modal_search_targets 가 self.page 존재는 확인하되 비었으면 넘어간다
    auto.log = _Logger()
    return auto


class DeathNoticeDetectionTest(unittest.TestCase):
    def test_detects_real_kb_death_notice_text(self):
        """대표가 찍어 보낸 실제 KB 팝업 문구 — 감지돼야 한다(양성)."""
        auto = _automation()
        el = _FakeElement(REAL_DEATH_NOTICE_TEXT)
        dialog = _FakeFrame({".w2modal": [el]})

        self.assertTrue(auto._detect_death_notice(dialog))

    def test_does_not_false_positive_on_unrelated_server_error(self):
        """사망자와 무관한 서버오류 팝업은 걸리면 안 된다(오탐 방지)."""
        auto = _automation()
        el = _FakeElement("서버와의 통신 중 예기치 못한 오류가 발생했습니다(-S0001).")
        dialog = _FakeFrame({".w2modal": [el]})

        self.assertFalse(auto._detect_death_notice(dialog))

    def test_does_not_false_positive_on_unrelated_consent_block(self):
        """'이미 서면 동의' 차단 알림도 사망자로 오분류되면 안 된다."""
        auto = _automation()
        el = _FakeElement("이미 서면 동의를 받은 고객이라 2개월 이후 출력하실 수 없습니다.")
        dialog = _FakeFrame({".w2modal": [el]})

        self.assertFalse(auto._detect_death_notice(dialog))

    def test_ignores_invisible_elements(self):
        """화면에 안 보이는(is_visible False) 엘리먼트는 무시한다."""
        auto = _automation()
        el = _FakeElement(REAL_DEATH_NOTICE_TEXT, visible=False)
        dialog = _FakeFrame({".w2modal": [el]})

        self.assertFalse(auto._detect_death_notice(dialog))

    def test_no_modal_present_returns_false(self):
        auto = _automation()
        dialog = _FakeFrame({})

        self.assertFalse(auto._detect_death_notice(dialog))


if __name__ == "__main__":
    unittest.main()
