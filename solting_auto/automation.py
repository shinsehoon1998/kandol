"""Playwright 기반 웹 자동화 엔진. (PRD FR-3.x)

솔팅프로그램 로그인 -> 등록 폼 입력 -> 제출 -> 결과 판별.
실제 셀렉터는 config.yaml 의 selectors 에서 주입한다.

playwright 는 메서드 내부에서 지연 import 하므로, dry-run(브라우저 미사용)
시에는 이 모듈을 import 해도 playwright 설치가 필요 없다.
"""

import time

from .validators import normalize_phone, normalize_digits


class RegisterError(Exception):
    """등록 실패 (재시도 대상이 아닌 업무적 실패)."""


class RetryableError(Exception):
    """네트워크/요소 타임아웃 등 재시도 가능한 오류."""


class DuplicateCustomerError(Exception):
    """최근 2개월 이내 중복 등록 이력 존재로 스킵하는 오류."""


class DataSkipError(Exception):
    """행 데이터 문제(이름 형식 오류 등)로 이 행만 SKIP(재시도/실패 아님, 서킷브레이커 제외)."""


class SoltingAutomation:
    def __init__(self, config: dict, logger):
        self.cfg = config
        self.log = logger
        self.sel = config["selectors"]
        self.run = config["run"]
        self.fmt = config.get("format", {})
        self._pw = None
        self._browser = None
        self.page = None

    # --- 컨텍스트 관리 ---
    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=self.run.get("headless", False))
        ctx = self._browser.new_context()
        self.page = ctx.new_page()
        self.page.set_default_timeout(self.run.get("element_timeout_ms", 10000))
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    # --- 로그인 (FR-3.1) ---
    def login(self):
        import solting_auto
        solting_auto.check_stop()

        site = self.cfg["site"]
        creds = self.cfg["credentials"]
        password = creds.get("_resolved_password")
        if not password:
            raise RegisterError("비밀번호가 설정되지 않았습니다(.env 또는 config 확인).")

        s = self.sel["login"]
        self.log.info("로그인 페이지 이동")
        self.page.goto(site["login_url"])
        self.page.fill(s["username"], creds["username"])
        self.page.fill(s["password"], password)
        self.page.click(s["submit"])

        # 성공 판별
        check = s.get("success_check")
        if check:
            try:
                self.page.wait_for_selector(check, timeout=self.run.get("element_timeout_ms", 10000))
            except Exception:
                raise RegisterError("로그인 실패(성공 요소 미발견) - 자격증명을 확인하세요.")
        self.log.info("로그인 성공")

    # --- 단일 건 등록 (FR-3.2, 3.3) ---
    def register_one(self, jumin: str, name: str, phone: str) -> None:
        """1건 등록. 성공 시 정상 반환, 실패 시 예외."""
        import solting_auto
        solting_auto.check_stop()

        site = self.cfg["site"]
        s = self.sel["register"]

        jumin_val = self._fmt_jumin(jumin)
        phone_val = self._fmt_phone(phone)

        self.page.goto(site["register_url"])
        self.page.fill(s["jumin"], jumin_val)
        self.page.fill(s["name"], str(name))
        self.page.fill(s["phone"], phone_val)
        self.page.click(s["submit"])

        # 결과 판별: 성공 요소 우선, 없으면 오류 요소 확인
        success_check = s.get("success_check")
        error_check = s.get("error_check")
        timeout = self.run.get("element_timeout_ms", 10000)

        if success_check:
            try:
                self.page.wait_for_selector(success_check, timeout=timeout)
                return  # 성공
            except Exception:
                pass
        if error_check and self.page.query_selector(error_check):
            txt = self.page.inner_text(error_check) if self.page.query_selector(error_check) else "등록 오류"
            raise RegisterError(f"등록 실패: {txt[:100]}")
        if not success_check:
            # 판별 요소가 설정 안 됐으면 보수적으로 실패 처리하지 않고 통과
            return
        raise RetryableError("등록 결과 확인 실패(응답 지연/요소 미발견)")

    def screenshot(self, path: str) -> str:
        """실패 증빙 스크린샷 (FR-5.4)."""
        try:
            self.page.screenshot(path=path, full_page=True)
            return path
        except Exception:
            return ""

    # --- 포맷 변환 ---
    def _fmt_jumin(self, jumin: str) -> str:
        digits = normalize_digits(jumin)
        if self.fmt.get("jumin_hyphen", True) and len(digits) == 13:
            return f"{digits[:6]}-{digits[6:]}"
        return digits

    def _fmt_phone(self, phone: str) -> str:
        digits = normalize_phone(phone)
        if self.fmt.get("phone_hyphen", True):
            if len(digits) == 11:
                return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
            if len(digits) == 10:
                return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"
        return digits
