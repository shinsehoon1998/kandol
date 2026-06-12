"""2단계: KB손해보험 GA/교차 전용 - 동의서 출력 자동화.

흐름(첨부 화면 기준):
  로그인(아이디+비밀번호+생년월일)
   → 좌측 '동의서 출력' 메뉴 클릭(팝업)
   → 고객명 + 주민등록번호 입력, 가입설계 체크 / 입력 / 단독입력
   → '출력' 클릭
   → (네트워크 가로채기로) 동의서 PDF 저장
"""

import json
import re
import time
from pathlib import Path

from .validators import normalize_phone, normalize_digits
from .automation import RegisterError, RetryableError, DuplicateCustomerError


def _safe(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", str(s or ""))


class InsuranceAutomation:
    @property
    def stop_requested(self):
        import solting_auto
        if solting_auto.is_stop_requested():
            return True
        return getattr(self, "_stop_requested", False)

    @stop_requested.setter
    def stop_requested(self, val):
        self._stop_requested = val

    def __init__(self, config: dict, logger):
        self.cfg = config
        self.ins = config["insurance"]
        self.log = logger
        self.run = config["run"]
        self.fmt = config.get("format", {})
        self.sel = self.ins["selectors"]
        self.pdf_dir = Path(self.ins.get("pdf_folder", "./output/consent_pdfs"))
        self.out_dir = Path(self.run.get("output_folder", "./output"))
        self.popup_mode = self.ins.get("popup_mode", "window")
        self.capture_mode = self.ins.get("pdf_capture", "network")
        self.url_keywords = [k.lower() for k in self.ins.get("pdf_url_keywords", [])]
        self.network_debug = self.ins.get("network_debug", False)
        self._pw = None
        self._browser = None
        self.context = None
        self.page = None
        self.stop_requested = False
        import solting_auto.insurance
        solting_auto.insurance.active_instance = self

    def __enter__(self):
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        browser = self.ins.get("browser", {})
        mode = browser.get("mode", "launch")

        if mode == "attach":
            cdp_url = browser.get("cdp_url", "http://localhost:9222")
            self.log.info(f"기존 브라우저에 접속(CDP): {cdp_url}")
            self._browser = self._pw.chromium.connect_over_cdp(cdp_url)
            self.context = (self._browser.contexts[0]
                            if self._browser.contexts else self._browser.new_context())
            
            self.page = None
            if self.context.pages:
                for p in self.context.pages:
                    url = p.url.lower()
                    if "main" in url or "wsserver" in url or "wq" in url:
                        self.page = p
                        self.log.info(f"메인 전산 페이지 발견 및 연결 성공: {p.url}")
                        break
                if not self.page:
                    self.page = self.context.pages[-1]
                    self.log.info(f"메인 키워드를 찾지 못해 가장 우측(최근) 탭에 연결합니다: {self.page.url}")
            else:
                self.page = self.context.new_page()
        else:
            channel = browser.get("channel")
            self._browser = self._pw.chromium.launch(
                channel=channel or None,
                headless=self.run.get("headless", False),
            )
            self.context = self._browser.new_context(accept_downloads=True)
            self.page = self.context.new_page()

        self.page.set_default_timeout(self.run.get("element_timeout_ms", 10000))
        
        def handle_dialog(dialog):
            self.log.info(f"[알림 감지] 브라우저 경고창 자동 수락 처리: {dialog.message}")
            dialog.accept()
        self.page.on("dialog", handle_dialog)

        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def login(self, username=None, password=None, birthdate=None, force=False):
        import solting_auto
        solting_auto.check_stop()

        browser = self.ins.get("browser", {})
        # attach 모드 + skip_login: 사용자가 이미 수동 로그인했다고 가정하고 건너뜀 (force=True인 경우 무시하고 진행)
        if browser.get("mode") == "attach" and browser.get("skip_login", True) and not force:
            self.log.info("attach 모드 - 수동 로그인 상태 가정(자동 로그인 건너뜀)")
            return

        creds = self.ins.get("credentials", {})
        final_username = username or creds.get("username")
        final_password = password or creds.get("_resolved_password")
        final_birth = str(birthdate if birthdate is not None else creds.get("birthdate", ""))

        if not final_username:
            raise RegisterError("KB 아이디가 설정되거나 전달되지 않았습니다.")
        if not final_password:
            raise RegisterError("KB 비밀번호가 설정되지 않았습니다(.env 의 INSURANCE_PASSWORD 또는 대시보드 입력을 확인하세요).")

        s = self.sel["login"]
        self.log.info("KB 로그인 페이지 이동")
        self.page.goto(self.ins["login_url"])

        # '개인고유번호(생년월일)' 탭 선택(이미 기본일 수 있음)
        self._try_click(s.get("tab_personal"))

        self.page.fill(s["username"], final_username)
        self.page.fill(s["password"], final_password)
        if s.get("birthdate") and final_birth:
            self.page.fill(s["birthdate"], final_birth)
        self.page.click(s["submit"])

        check = s.get("success_check")
        menu_check = self.sel["consent"].get("menu")
        self.log.info("로그인 버튼 클릭 완료. 메인 화면 전환 및 새 탭 감지 대기 중 (최대 20초)...")
        
        success = False
        start_time = time.time()
        while time.time() - start_time < 20:
            # 1) 현재 열린 모든 탭(페이지) 중 메인 키워드가 포함된 탭이 생겼는지 실시간 스캔 및 연결 전환
            for p in self.context.pages:
                try:
                    url = p.url.lower()
                    if "main" in url or "wsserver" in url or "wq" in url:
                        self.page = p
                        self.log.info(f"성공적으로 메인 전산 페이지 탭을 감지하여 연결을 전환했습니다: {p.url}")
                        success = True
                        break
                except Exception:
                    continue
            if success:
                break
                
            # 2) 기존 탭 자체 내에서 성공 요소가 나타났는지 1초 미만 단위로 체크
            for selector in [check, menu_check, "text=로그아웃"]:
                if not selector:
                    continue
                try:
                    if self.page.locator(selector).count() > 0 and self.page.locator(selector).first.is_visible():
                        self.log.info(f"현재 페이지에서 성공 요소 발견: {selector}")
                        success = True
                        break
                except Exception:
                    continue
            if success:
                break
                
            time.sleep(0.5)
                
        if not success:
            current_url = self.page.url
            if "main" in current_url or "wsserver" in current_url or "wq" in current_url:
                self.log.info(f"성공 요소를 찾지 못했으나 URL이 메인 페이지로 변경됨을 확인했습니다: {current_url}")
            else:
                raise RegisterError(
                    "KB 로그인 실패(성공 요소 또는 메인 탭 미발견). 자격증명 또는 수동 로그인을 확인해 주세요."
                )
        
        time.sleep(1.0)
        self.log.info("KB 로그인 성공")

    def register_and_consent(self, jumin: str, name: str, phone: str) -> str:
        import solting_auto
        solting_auto.check_stop()

        dialog = self._open_consent_dialog()

        try:
            # 입력 (주민번호 먼저 입력 후 고객명 기입으로 순서 정상화)
            self._fill_smart(dialog, self.sel["consent"].get("jumin"), self._fmt_jumin(jumin), "주민번호")
            self._fill_smart(dialog, self.sel["consent"].get("name"), str(name), "이름")
            
            # 입력 후 1.0초 이내에 중복 팝업이 뜨는지 감지
            is_dup = False
            for _ in range(5):
                solting_auto.check_stop()
                if self._check_duplicate_popup(dialog):
                    is_dup = True
                    break
                time.sleep(0.2)
                
            if is_dup:
                self.log.info(f"[{name}] 최근 2개월 이내 입력 이력이 존재하여 등록을 건너뜁니다.")
                raise DuplicateCustomerError("최근 등록 이력 존재(2개월 이내)")

            # 옵션(가입설계 체크 / 입력 / 단독입력) - 이미 기본값이면 실패해도 무시
            self._try_check(dialog, self.sel["consent"].get("agree_check"))
            self._try_click_on(dialog, self.sel["consent"].get("info_output_input"))
            self._try_click_on(dialog, self.sel["consent"].get("single_input"))

            # 출력 + PDF/PNG 캡처
            res_path = self._print_and_capture(dialog, name, phone, jumin)
            return res_path
        finally:
            self._close_consent_dialog(dialog)

    def register_and_consent_batch(self, batch_records: list, file_format: str = None) -> list:
        """다중입력 모드로 여러 고객(최대 10명)을 일괄 입력 및 출력 처리합니다.
        batch_records: [{'jumin': '...', 'name': '...', 'phone': '...', 'row_no': ...}, ...]
        file_format: 'PDF' 또는 'PNG'
        """
        import solting_auto
        solting_auto.check_stop()

        dialog = self._open_consent_dialog()
        results = []
        registered_records = []
        
        try:
            # 1) '다중입력' 라디오 버튼 클릭
            self.log.info("입력 방식을 '다중입력'으로 변경합니다.")
            self._try_click_on(dialog, self.sel["consent"].get("multi_input", "text=다중입력"))
            time.sleep(0.5)

            # 2) '고객정보출력여부=입력' 라디오 버튼 클릭
            self._try_click_on(dialog, self.sel["consent"].get("info_output_input", "text=입력"))
            time.sleep(0.3)
            
            # 3) 각 레코드 입력 처리
            for i, rec in enumerate(batch_records):
                solting_auto.check_stop()
                
                # 셀렉터 포맷팅
                jumin_sel = self.sel["consent"].get("multi_jumin_format", "[id*='iptCustIdno_{i}']").replace("{i}", str(i))
                name_sel = self.sel["consent"].get("multi_name_format", "[id*='iptCustNm_{i}']").replace("{i}", str(i))
                
                self.log.info(f"[{rec['name']}] 다중입력 슬롯 {i}번에 입력 중...")
                
                # 입력 실행
                self._fill_smart(dialog, jumin_sel, self._fmt_jumin(rec['jumin']), f"주민번호 {i}")
                self._fill_smart(dialog, name_sel, str(rec['name']), f"이름 {i}")
                
                # 중복 팝업 실시간 감지 (1초 대기하며 감지)
                is_dup = False
                for _ in range(5):
                    solting_auto.check_stop()
                    if self._check_duplicate_popup(dialog):
                        is_dup = True
                        break
                    time.sleep(0.2)
                    
                if is_dup:
                    self.log.info(f"[{rec['name']}] 중복 경고 모달 감지됨. 이 레코드는 건너뜁니다.")
                    # 입력했던 슬롯 비우기
                    self._clear_input(dialog, name_sel)
                    self._clear_input(dialog, jumin_sel)
                    results.append({
                        "row_no": rec["row_no"],
                        "jumin": rec["jumin"],
                        "name": rec["name"],
                        "phone": rec["phone"],
                        "status": SKIP,
                        "reason": "최근 등록 이력 존재(2개월 이내)",
                        "pdf_path": ""
                    })
                else:
                    # 체크박스 선택 (chkGrid_i)
                    chk_sel = f"[id*='chkGrid_{i}']"
                    self._try_check(dialog, chk_sel)
                    
                    registered_records.append(rec)
                    results.append({
                        "row_no": rec["row_no"],
                        "jumin": rec["jumin"],
                        "name": rec["name"],
                        "phone": rec["phone"],
                        "status": SUCCESS,
                        "pdf_path": ""  # 캡처 완료 후 채워짐
                    })

            solting_auto.check_stop()
            
            if not registered_records:
                self.log.info("이번 배치에서 등록할 수 있는 고객이 없습니다. 출력을 건너뜁니다.")
                return results

            # 4) 일괄 출력 및 캡처
            actual_format = file_format or self.ins.get("oz", {}).get("file_format", "PDF")
            ext = ".png" if "PNG" in actual_format.upper() else ".pdf"
            
            batch_id = int(time.time())
            temp_dest = self.pdf_dir / f"batch_temp_{batch_id}{ext}"
            
            self.log.info(f"등록 성공 고객 {len(registered_records)}명에 대한 일괄 출력 및 저장 시작 (형식: {actual_format})")
            
            print_btn = self.sel["consent"].get("print_btn")
            
            if self.capture_mode == "oz_windows":
                from . import oz_viewer
                try:
                    self._click_print_btn(dialog, print_btn)
                except Exception as e:
                    raise RetryableError(f"'출력' 버튼 클릭 실패: {e}")
                try:
                    path = oz_viewer.save_as_pdf(str(temp_dest), self.ins.get("oz", {}), self.log, file_format=actual_format)
                except Exception as e:
                    raise RetryableError(f"OZ 뷰어 일괄 {actual_format} 저장 실패: {e}")
            else:
                raise RegisterError("다중입력 모드는 OZ Windows 저장 모드(oz_windows)만 지원합니다.")

            # 5) 생성된 파일 검증 및 개별 분할 저장
            if "PNG" in actual_format.upper():
                total_pages = len(registered_records) * 3
                self.log.info(f"일괄 출력 이미지 파일들을 개별 고객(인당 3장)으로 매핑합니다. 총 예상 페이지: {total_pages}장")
                
                reg_idx = 0
                for r_item in results:
                    if r_item["status"] == SUCCESS:
                        name = r_item["name"]
                        phone = r_item["phone"]
                        jumin = r_item["jumin"]
                        suffix = (normalize_phone(phone)[-4:] if normalize_phone(phone)
                                  else normalize_digits(jumin)[:6])
                        
                        cust_base = self.pdf_dir / f"동의서_{_safe(name)}_{suffix}.png"
                        
                        src_pages = [
                            self.pdf_dir / f"{temp_dest.stem}_{3 * reg_idx + 1}.png",
                            self.pdf_dir / f"{temp_dest.stem}_{3 * reg_idx + 2}.png",
                            self.pdf_dir / f"{temp_dest.stem}_{3 * reg_idx + 3}.png"
                        ]
                        
                        dest_pages = [
                            self.pdf_dir / f"동의서_{_safe(name)}_{suffix}_1.png",
                            self.pdf_dir / f"동의서_{_safe(name)}_{suffix}_2.png",
                            self.pdf_dir / f"동의서_{_safe(name)}_{suffix}_3.png"
                        ]
                        
                        missing = []
                        for src_p in src_pages:
                            if not src_p.exists():
                                missing.append(src_p.name)
                                
                        if missing:
                            self.log.error(f"[{name}] 이미지 파일 누락 감지: {missing}")
                            r_item["status"] = FAIL
                            r_item["reason"] = f"출력 이미지 파일 누락: {', '.join(missing)}"
                        else:
                            import shutil
                            for src_p, dest_p in zip(src_pages, dest_pages):
                                shutil.copy2(src_p, dest_p)
                                try:
                                    src_p.unlink()
                                except:
                                    pass
                            
                            r_item["pdf_path"] = str(cust_base)
                            self.log.info(f"[{name}] 개별 이미지 3장 매핑 완료: {cust_base.name}")
                            
                        reg_idx += 1
            else:
                temp_pdf = Path(path)
                if not temp_pdf.exists():
                    raise RegisterError(f"일괄 출력 PDF 파일이 생성되지 않았습니다: {temp_dest}")
                
                import fitz
                doc = fitz.open(temp_pdf)
                num_pages = len(doc)
                doc.close()
                
                expected_pages = len(registered_records) * 3
                self.log.info(f"일괄 출력 PDF ({num_pages}페이지)를 개별 고객(인당 3페이지)으로 분할합니다. 예상: {expected_pages}페이지")
                
                reg_idx = 0
                for r_item in results:
                    if r_item["status"] == SUCCESS:
                        name = r_item["name"]
                        phone = r_item["phone"]
                        jumin = r_item["jumin"]
                        suffix = (normalize_phone(phone)[-4:] if normalize_phone(phone)
                                  else normalize_digits(jumin)[:6])
                        
                        cust_dest = self.pdf_dir / f"동의서_{_safe(name)}_{suffix}.pdf"
                        
                        start_page = reg_idx * 3
                        end_page = start_page + 3
                        
                        if start_page < num_pages:
                            doc_all = fitz.open(temp_pdf)
                            new_doc = fitz.open()
                            new_doc.insert_pdf(doc_all, from_page=start_page, to_page=min(end_page - 1, num_pages - 1))
                            new_doc.save(str(cust_dest))
                            new_doc.close()
                            doc_all.close()
                            
                            r_item["pdf_path"] = str(cust_dest)
                            self.log.info(f"[{name}] PDF 분할 완료: {cust_dest.name}")
                        else:
                            r_item["status"] = FAIL
                            r_item["reason"] = "출력 PDF 내 페이지 부족"
                            
                        reg_idx += 1
                        
                try:
                    temp_pdf.unlink()
                except:
                    pass
            
            return results
            
        finally:
            self._close_consent_dialog(dialog)

    def _check_duplicate_popup(self, dialog) -> bool:
        """최근 2개월 이내 입력 이력이 있는 중복 고객 팝업이 뜨는지 감지하고, '아니오'를 눌러 닫습니다."""
        modal_sel = self.sel["consent"].get("duplicate_modal", "div.popup_confirm")
        no_btn_sel = self.sel["consent"].get("duplicate_no_btn", "button:has-text('아니오')")
        
        if not modal_sel:
            return False
            
        frames_to_search = [self.page] + list(self.page.frames)
        for f in frames_to_search:
            try:
                loc = f.locator(modal_sel)
                if loc.count() > 0 and loc.first.is_visible():
                    text = (loc.first.inner_text() or "").strip()
                    self.log.info(f"[중복 감지] 모달 발견! 텍스트: {text}")
                    
                    no_btn = f.locator(no_btn_sel)
                    if no_btn.count() > 0 and no_btn.first.is_visible():
                        self.log.info("[중복 감지] '아니오' 버튼 클릭하여 팝업을 닫습니다.")
                        no_btn.first.click(force=True)
                        time.sleep(1.0)
                        return True
                    else:
                        self.log.warning("[중복 감지] 지정된 셀렉터로 '아니오' 버튼을 찾지 못해 텍스트 매칭으로 재시도합니다.")
                        for alt_btn in ["button:has-text('아니오')", "a:has-text('아니오')", "input[value='아니오']", "text='아니오'"]:
                            btn_loc = f.locator(alt_btn)
                            if btn_loc.count() > 0 and btn_loc.first.is_visible():
                                btn_loc.first.click(force=True)
                                time.sleep(1.0)
                                return True
            except Exception:
                continue
        return False

    def _clear_input(self, dialog, selector):
        if not selector:
            return
        frames_to_search = [self.page] + list(self.page.frames)
        for f in frames_to_search:
            try:
                loc = f.locator(selector)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(force=True, timeout=500)
                    loc.first.evaluate("el => el.focus()")
                    loc.first.press("Control+A")
                    loc.first.press("Backspace")
                    loc.first.evaluate("""e => {
                        e.blur();
                        e.dispatchEvent(new Event('change', { bubbles: true }));
                        e.dispatchEvent(new Event('input', { bubbles: true }));
                    }""")
                    return
            except Exception:
                continue

    def _open_consent_dialog(self):
        """좌측 '동의서 출력' 메뉴를 눌러 팝업/모달을 연다. 작업 대상 page/frame 반환."""
        import solting_auto
        solting_auto.check_stop()

        menu = self.sel["consent"].get("menu")
        
        # 1) 기존에 열려 있는 모달이 있다면 닫아서 상태를 리셋합니다.
        self.log.info("이전 작업으로 인해 열려 있는 동의서 출력 모달이 있는지 확인합니다...")
        self._close_consent_dialog(None)
                
        # 2) 팝업 창 감지 시도 (expect_page)
        popup = None
        self.log.info(f"동의서 출력 메뉴 클릭 및 팝업 탐지 시작 (셀렉터: {menu})")
        
        try:
            with self.context.expect_page(timeout=3000) as pinfo:
                success = self._click_in_frames(menu, timeout=5000)
                if not success:
                    raise RegisterError(f"메뉴 '{menu}'를 찾을 수 없습니다.")
            popup = pinfo.value
            popup.wait_for_load_state()
            self.log.info("새 브라우저 팝업 창이 감지되었습니다.")
            return popup
        except Exception as e:
            if "찾을 수 없습니다" in str(e):
                raise RegisterError(str(e))
                
            self.log.info(f"새 브라우저 창이 감지되지 않았습니다. (동적 모달로 진행): {e}")
            self.log.info("모달 요소가 로드될 때까지 2초 대기합니다...")
            time.sleep(2)
            
            best_frame = None
            best_score = 0
            
            frames_to_check = [self.page] + list(self.page.frames)
            for f in frames_to_check:
                try:
                    inputs = f.query_selector_all("input")
                    score = 0
                    for el in inputs:
                        try:
                            if not el.is_visible():
                                continue
                        except Exception:
                            continue
                            
                        el_type = el.get_attribute("type") or ""
                        if el_type.lower() in ["radio", "checkbox", "hidden", "file"]:
                            continue
                        el_id = (el.get_attribute("id") or "").lower()
                        el_name = (el.get_attribute("name") or "").lower()
                        el_class = (el.get_attribute("class") or "").lower()
                        el_placeholder = (el.get_attribute("placeholder") or "")
                        
                        keywords = ["name", "nm", "cust", "cst", "cstnm", "custnm", "이름", "고객", "성명",
                                    "jumin", "ssn", "res", "rn", "rrn", "resno", "idno", "custid", "custidno", "주민", "등록", "번호"]
                        for kw in keywords:
                            if kw in el_id or kw in el_name or kw in el_class or kw in el_placeholder:
                                score += 10
                                
                    if score > best_score:
                        best_score = score
                        best_frame = f
                except Exception:
                    continue
                    
            if best_frame:
                if best_frame == self.page:
                    self.log.info(f"메인 페이지에서 모달 입력 영역을 감지했습니다. (매칭 점수: {best_score})")
                else:
                    self.log.info(f"프레임 '{best_frame.name}'에서 모달 입력 영역을 감지했습니다. (url: {best_frame.url}, 매칭 점수: {best_score})")
                return best_frame
                
            for frame in self.page.frames:
                if "main" in frame.name.lower() or "main" in frame.url.lower():
                    self.log.info(f"모달을 찾지 못해 'main' 키워드 프레임을 반환합니다: {frame.name}")
                    return frame
                    
            self.log.warning("모달이 있는 프레임을 감지하지 못해 메인 페이지를 반환합니다.")
            return self.page

    def _close_consent_dialog(self, dialog):
        """동의서 출력 완료 후 켜져 있는 팝업 창 또는 모달을 닫는다."""
        if dialog is not None:
            try:
                # Case A: dialog가 별도 팝업 페이지인 경우
                if hasattr(dialog, "close") and hasattr(dialog, "is_closed") and not dialog.is_closed():
                    self.log.info("팝업 페이지를 닫습니다.")
                    dialog.close()
                    time.sleep(1.0)
                    return
            except Exception as e:
                self.log.debug(f"팝업 close() 실패: {e}")
        
        # Case B: dialog가 메인 페이지 내 모달(Frame)인 경우
        self.log.info("모달 창을 닫기 위해 '닫기' 및 '창닫기(X)' 버튼을 탐색합니다...")
        try:
            frames_to_search = [self.page] + list(self.page.frames)
            for f in frames_to_search:
                try:
                    # 1) '닫기' 텍스트/값/태그를 가진 엘리먼트 통합 탐색 (가장 흔함)
                    selectors = ["input[value='닫기']", "button:has-text('닫기')", "a:has-text('닫기')", "text='닫기'"]
                    for selector in selectors:
                        loc = f.locator(selector)
                        cnt = loc.count()
                        for i in range(cnt):
                            el = loc.nth(i)
                            if el.is_visible():
                                el_id = el.get_attribute("id") or ""
                                if "btnCloseAll" in el_id:
                                    continue  # 탭 전체 닫기 단추 제외
                                self.log.info(f"모달 '닫기' 버튼 클릭 시도 (ID: {el_id}, Selector: {selector})")
                                try:
                                    el.click(force=True, timeout=2000)
                                except Exception as click_err:
                                    self.log.warning(f"일반 click 실패, JS click 시도: {click_err}")
                                    el.evaluate("el => el.click()")
                                time.sleep(1.5)
                                return
                            
                    # 2) '창닫기(X)' 헤더 버튼 탐색 (.w2window_close 클래스 또는 title 속성)
                    close_x = f.locator(".w2window_close, [title*='창닫기'], [title*='닫기']")
                    x_cnt = close_x.count()
                    for i in range(x_cnt):
                        el = close_x.nth(i)
                        if el.is_visible():
                            el_id = el.get_attribute("id") or ""
                            self.log.info(f"모달 'X' 닫기 버튼 클릭 시도 (ID: {el_id})")
                            try:
                                el.click(force=True, timeout=2000)
                            except Exception as click_err:
                                self.log.warning(f"일반 click 실패, JS click 시도: {click_err}")
                                el.evaluate("el => el.click()")
                            time.sleep(1.5)
                            return
                except Exception as inner_e:
                    self.log.debug(f"프레임 내 닫기 버튼 검색 실패: {inner_e}")
                    continue
        except Exception as e:
            self.log.warning(f"모달 닫기 중 오류: {e}")

    def _print_and_capture(self, dialog, name, phone, jumin, file_format=None) -> str:
        import solting_auto
        solting_auto.check_stop()

        self.pdf_dir.mkdir(parents=True, exist_ok=True)
        suffix = (normalize_phone(phone)[-4:] if normalize_phone(phone)
                  else normalize_digits(jumin)[:6])
        
        # 파일 형식 파악 및 확장자 매칭
        actual_format = file_format or self.ins.get("oz", {}).get("file_format", "PDF")
        ext = ".png" if "PNG" in actual_format.upper() else ".pdf"
        dest = self.pdf_dir / f"동의서_{_safe(name)}_{suffix}{ext}"
        print_btn = self.sel["consent"].get("print_btn")
        timeout = self.run.get("element_timeout_ms", 10000)

        if self.capture_mode == "oz_windows":
            from . import oz_viewer
            try:
                self._click_print_btn(dialog, print_btn)
            except Exception as e:
                raise RetryableError(f"'출력' 버튼 클릭 실패: {e}")
            try:
                path = oz_viewer.save_as_pdf(str(dest), self.ins.get("oz", {}), self.log, file_format=actual_format)
            except Exception as e:
                raise RetryableError(f"OZ 뷰어 저장 실패: {e}")
            return self._verify(Path(path))

        if self.capture_mode == "download":
            try:
                with dialog.expect_download(timeout=timeout) as dl:
                    self._click_print_btn(dialog, print_btn)
                dl.value.save_as(str(dest))
            except Exception as e:
                raise RetryableError(f"동의서 PDF 다운로드 실패: {e}")
            return self._verify(dest)

        candidates = []
        debug_log = []

        def on_response(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "").lower()
                url = resp.url
                if self.network_debug:
                    debug_log.append({"url": url, "status": resp.status, "content_type": ct})
                score = 0
                if "application/pdf" in ct:
                    score = 100
                elif "octet-stream" in ct:
                    score = 60
                if any(k in url.lower() for k in self.url_keywords):
                    score += 20
                if score > 0:
                    try:
                        body = resp.body()
                    except Exception:
                        body = b""
                    candidates.append((score, ct, url, body))
            except Exception:
                pass

        self.page.on("response", on_response)
        dialog.on("response", on_response) if dialog is not self.page else None
        try:
            self._click_print_btn(dialog, print_btn)
            t_left = min(timeout, 8000)
            while t_left > 0:
                solting_auto.check_stop()
                chunk = min(t_left, 500)
                dialog.wait_for_timeout(chunk)
                t_left -= chunk
        finally:
            try:
                self.page.remove_listener("response", on_response)
                if dialog is not self.page:
                    dialog.remove_listener("response", on_response)
            except Exception:
                pass

        if self.network_debug:
            dbg = self.out_dir / f"oz_network_{_safe(name)}_{suffix}.json"
            dbg.parent.mkdir(parents=True, exist_ok=True)
            dbg.write_text(json.dumps(debug_log, ensure_ascii=False, indent=2), encoding="utf-8")
            self.log.info(f"네트워크 진단 덤프 저장: {dbg.name} (요청 {len(debug_log)}건)")

        pdfs = [c for c in candidates if c[3][:4] == b"%PDF"]
        pick = max(pdfs or candidates, key=lambda c: c[0]) if (pdfs or candidates) else None
        if not pick or not pick[3]:
            raise RetryableError(
                "동의서 PDF 응답을 찾지 못했습니다. 출력이 OZ 뷰어로 열렸을 가능성이 큽니다."
            )
        dest.write_bytes(pick[3])
        self.log.info(f"동의서 PDF 저장: {dest.name} ({pick[1]})")
        return self._verify(dest)

    def _verify(self, dest: Path) -> str:
        is_png = dest.suffix.lower() == ".png"
        if is_png:
            first_page = dest.parent / f"{dest.stem}_1.png"
            if not first_page.exists() or first_page.stat().st_size == 0:
                raise RetryableError("동의서 PNG 이미지 파일이 저장되지 않았습니다.")
        else:
            if not dest.exists() or dest.stat().st_size == 0:
                raise RetryableError("동의서 PDF가 저장되지 않았습니다.")
        return str(dest)

    def screenshot(self, path: str) -> str:
        try:
            self.page.screenshot(path=path, full_page=True)
            return path
        except Exception:
            return ""

    # --- 입력 헬퍼 ---
    def _fill(self, target, selector, value):
        if not selector:
            return
        target.fill(selector, value)

    def _try_click(self, selector):
        if not selector:
            return
        try:
            self.page.click(selector, timeout=2500)
        except Exception:
            pass

    def _try_click_on(self, target, selector):
        if not selector:
            return
        frames_to_search = [self.page] + list(self.page.frames)
        for f in frames_to_search:
            try:
                loc = f.locator(selector)
                if loc.count() > 0 and loc.first.is_visible():
                    loc.first.click(timeout=2500)
                    self.log.info(f"성공적으로 요소를 클릭했습니다: selector={selector}")
                    return
            except Exception:
                continue

    def _try_check(self, target, selector):
        if not selector:
            return
        frames_to_search = [self.page] + list(self.page.frames)
        for f in frames_to_search:
            try:
                loc = f.locator(selector)
                if loc.count() > 0 and loc.first.is_visible():
                    try:
                        loc.first.check(timeout=2500)
                    except Exception:
                        loc.first.click(timeout=2500)
                    self.log.info(f"성공적으로 요소를 체크했습니다: selector={selector}")
                    return
            except Exception:
                continue

    # --- 포맷 변환 ---
    def _fmt_jumin(self, jumin: str) -> str:
        digits = normalize_digits(jumin)
        if self.fmt.get("jumin_hyphen", True) and len(digits) == 13:
            return f"{digits[:6]}-{digits[6:]}"
        return digits

    # --- 자가 치유형 (Self-healing) 자동 입력/클릭 헬퍼 ---
    def _fill_smart(self, target, selector, value, field_type):
        """자가 치유형 입력 필러. 셀렉터가 실패하거나 다른 프레임에 있으면 스캔하여 사람처럼 한글자씩 입력합니다."""
        import solting_auto
        solting_auto.check_stop()

        # 0) 마우스 클릭을 방해하는 웹스퀘어 로딩 디머 레이어를 강제로 숨김 (display = 'none')
        try:
            self.page.evaluate("""() => {
                const dims = document.querySelectorAll("[id*='processMsg_modalPopupDim'], .w2group");
                dims.forEach(el => {
                    if (el && (el.id.includes("Dim") || el.className.includes("Dim") || el.id.includes("processMsg"))) {
                        el.style.display = 'none';
                        el.style.visibility = 'hidden';
                        el.style.zIndex = '-9999';
                    }
                });
            }""")
            self.log.info("클릭 방해 로딩 디머 레이어를 강제로 숨김(display=none) 처리 완료했습니다.")
        except Exception as e:
            self.log.warning(f"디머 레이어 숨김 처리 실패(계속 진행): {e}")

        # [최적화] 주민등록번호와 이름 모두 정밀 CSS 셀렉터(id*=...) 체제로 단일화하여 신속하고 안전하게 기입합니다.
        # 1) 셀렉터 기반 다이렉트 프레임 재귀 검색 기입
        if selector:
            frames_to_search = [self.page] + list(self.page.frames)
            for f in frames_to_search:
                try:
                    loc = f.locator(selector)
                    if loc.count() > 0 and loc.first.is_visible():
                        self.log.info(f"[{field_type}] 셀렉터 '{selector}' 발견. 강제 포커싱 및 물리 타이핑을 시작합니다.")
                        
                        # 1. 클릭 시도 (절대 좌표 또는 일반 클릭)
                        try:
                            box = loc.first.bounding_box()
                            if box:
                                cx = box["x"] + box["width"] / 2
                                cy = box["y"] + box["height"] / 2
                                self.log.info(f"[{field_type}] 절대 좌표 ({cx}, {cy})로 마우스 강제 물리 클릭을 실행합니다.")
                                self.page.mouse.click(cx, cy)
                            else:
                                loc.first.click(force=True, timeout=500)
                        except Exception as click_err:
                            self.log.warning(f"[{field_type}] 클릭 실패했으나 강제 포커싱 진행: {click_err}")
                            
                        # 2. 강제 focus 스크립트 실행
                        loc.first.evaluate("el => el.focus()")
                        time.sleep(0.3)
                        
                        # 3. 기존 값 제거
                        loc.first.press("Control+A")
                        loc.first.press("Backspace")
                        time.sleep(0.1)
                        
                        # 4. 물리 keyboard 타이핑 (180ms 딜레이)
                        for char in str(value):
                            self.page.keyboard.type(char)
                            time.sleep(0.18)
                            
                        time.sleep(0.2)
                        
                        # 5. 확실한 blur 및 change/input 이벤트 강제 전달로 데이터 바인딩 트리거 (포커스 튐 방지)
                        loc.first.evaluate("""e => {
                            e.blur();
                            e.dispatchEvent(new Event('change', { bubbles: true }));
                            e.dispatchEvent(new Event('input', { bubbles: true }));
                        }""")
                        time.sleep(0.2)
                        self.log.info(f"[{field_type}] 모든 프레임 탐색을 통해 셀렉터 '{selector}'에 물리 타이핑 및 이벤트 디스패치를 완료했습니다.")
                        return
                except Exception as e:
                    self.log.warning(f"[{field_type}] 셀렉터 '{selector}' 입력 도중 오류 발생, 계속 진행: {e}")
                    continue
            self.log.info(f"[{field_type}] 모든 프레임에서 셀렉터 '{selector}'를 찾을 수 없어 자가 치유(Self-healing) 검색을 가동합니다.")
                
        # 2) 자가 치유 검색: 모든 프레임의 모든 input 요소를 스캔하여 매칭 점수가 가장 높은 input을 찾음
        best_el = None
        best_score = 0
        best_frame = None
        frames_to_search = [self.page] + list(self.page.frames)
        
        for f in frames_to_search:
            try:
                inputs = f.query_selector_all("input")
                for el in inputs:
                    try:
                        if not el.is_visible():
                            continue
                    except Exception:
                        continue
                        
                    el_type = el.get_attribute("type") or ""
                    if el_type.lower() in ["radio", "checkbox", "hidden", "file"]:
                        continue
                        
                    el_id = (el.get_attribute("id") or "").lower()
                    el_name = (el.get_attribute("name") or "").lower()
                    el_class = (el.get_attribute("class") or "").lower()
                    el_placeholder = (el.get_attribute("placeholder") or "")
                    
                    score = 0
                    if field_type == "이름":
                        for kw in ["name", "nm", "cust", "cst", "cstnm", "custnm", "이름", "고객", "성명"]:
                            if kw in el_id or kw in el_name or kw in el_class or kw in el_placeholder:
                                score += 10
                        if el_type.lower() == "text":
                            score += 2
                    elif field_type == "주민번호":
                        for kw in ["jumin", "ssn", "res", "rn", "rrn", "resno", "idno", "custid", "custidno", "주민", "등록", "번호"]:
                            if kw in el_id or kw in el_name or kw in el_class or kw in el_placeholder:
                                score += 10
                        if el_type.lower() in ["password", "text"]:
                            score += 2
                            
                    if score > best_score:
                        best_score = score
                        best_el = el
                        best_frame = f
            except Exception:
                continue
                
        if best_el:
            self.log.info(f"[{field_type}] 자가 치유 성공! 최적 요소를 발견하여 강제 클릭 및 물리 타이핑을 실행합니다. (프레임: '{best_frame.name}', 점수: {best_score})")
            try:
                try:
                    box = best_el.bounding_box()
                    if box:
                        cx = box["x"] + box["width"] / 2
                        cy = box["y"] + box["height"] / 2
                        self.log.info(f"[{field_type}] 절대 좌표 ({cx}, {cy})로 마우스 강제 물리 클릭을 실행합니다.")
                        self.page.mouse.click(cx, cy)
                    else:
                        best_el.click(force=True, timeout=500)
                except Exception as click_err:
                    self.log.warning(f"[{field_type}] 자가치유 요소 클릭 실패했으나 강제 포커싱 진행: {click_err}")
                    
                best_el.evaluate("el => el.focus()")
                time.sleep(0.3)
                
                best_el.press("Control+A")
                best_el.press("Backspace")
                time.sleep(0.1)
                
                for char in str(value):
                    self.page.keyboard.type(char)
                    time.sleep(0.18)
                    
                time.sleep(0.2)
                best_el.evaluate("""e => {
                    e.blur();
                    e.dispatchEvent(new Event('change', { bubbles: true }));
                    e.dispatchEvent(new Event('input', { bubbles: true }));
                }""")
                time.sleep(0.2)
                self.log.info(f"[{field_type}] 자가 치유 요소를 통해 물리 타이핑 및 이벤트 디스패치를 완료했습니다.")
            except Exception as e:
                self.log.warning(f"[{field_type}] 물리 타이핑 실패로 일반 fill로 폴백합니다: {e}")
                try:
                    best_el.fill(value)
                    best_el.evaluate("""e => {
                        e.blur();
                        e.dispatchEvent(new Event('change', { bubbles: true }));
                        e.dispatchEvent(new Event('input', { bubbles: true }));
                    }""")
                except Exception as fill_err:
                    self.log.error(f"[{field_type}] 폴백 일반 fill도 실패했습니다: {fill_err}")
                    raise RegisterError(f"[{field_type}] 입력에 완전히 실패했습니다: {fill_err}")
        else:
            raise RegisterError(f"[{field_type}] 입력창을 모든 프레임에서 찾을 수 없습니다.")

    def _click_smart(self, target, selector, action_name):
        """자가 치유형 클릭커. 셀렉터가 실패하면 모든 프레임에서 매칭 텍스트 및 요소를 기반으로 클릭합니다."""
        import solting_auto
        solting_auto.check_stop()

        if selector:
            frames_to_search = [self.page] + list(self.page.frames)
            for f in frames_to_search:
                try:
                    loc = f.locator(selector)
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click(force=True, timeout=3000)
                        self.log.info(f"[{action_name}] 모든 프레임 탐색을 통해 셀렉터 '{selector}'를 클릭했습니다.")
                        return
                except Exception as click_err:
                    self.log.warning(f"[{action_name}] 프레임 '{f.name}'에서 셀렉터 '{selector}' 클릭 실패 (스킵/계속): {click_err}")
                    continue
            self.log.info(f"[{action_name}] 모든 프레임에서 셀렉터 '{selector}'를 찾을 수 없어 자가 치유(Self-healing) 검색을 가동합니다.")

        keywords = ["출력", "인쇄", "인쇄하기", "출력하기", "저장", "프린트"]
        frames_to_search = [self.page] + list(self.page.frames)
        
        for f in frames_to_search:
            for kw in keywords:
                try:
                    loc = f.locator(f"text={kw}")
                    if loc.count() > 0 and loc.first.is_visible():
                        loc.first.click(force=True, timeout=2000)
                        self.log.info(f"[{action_name}] 프레임 '{f.name}'에서 '{kw}' 텍스트 매칭 클릭 성공!")
                        return
                except Exception as click_err:
                    continue
                    
        best_el = None
        best_score = 0
        best_frame = None
        
        for f in frames_to_search:
            try:
                elements = f.query_selector_all("button, a, input[type='button'], input[type='image']")
                for el in elements:
                    try:
                        if not el.is_visible():
                            continue
                    except Exception:
                        continue
                        
                    el_id = (el.get_attribute("id") or "").lower()
                    el_class = (el.get_attribute("class") or "").lower()
                    text = (el.inner_text() or "").strip()
                    
                    score = 0
                    for kw in ["print", "save", "btn", "submit", "prt"]:
                        if kw in el_id or kw in el_class:
                            score += 5
                    for kw in keywords:
                        if kw in text:
                            score += 10
                            
                    if score > best_score:
                        best_score = score
                        best_el = el
                        best_frame = f
            except Exception:
                continue
                
        if best_el:
            self.log.info(f"[{action_name}] 자가 치유 클릭 성공! 최적 요소를 클릭합니다. (프레임: '{best_frame.name}', 점수: {best_score})")
            try:
                best_el.click(force=True, timeout=3000)
            except Exception as click_err:
                self.log.error(f"[{action_name}] 자가 치유 최적 요소 클릭도 최종 실패: {click_err}")
                raise RegisterError(f"[{action_name}] 클릭 실패: {click_err}")
        else:
            raise RegisterError(f"[{action_name}] 버튼을 모든 프레임에서 찾을 수 없습니다.")

    def _click_print_btn(self, dialog, print_btn):
        self._click_smart(dialog, print_btn, "출력 버튼")

    def _click_in_frames(self, selector, timeout=3000, force=False):
        """메인 페이지 및 모든 서브 프레임(iframe)을 뒤져서 엘리먼트를 클릭합니다."""
        try:
            self.page.click(selector, timeout=2000, force=force)
            return True
        except Exception:
            pass
            
        sorted_frames = sorted(
            [f for f in self.page.frames if f != self.page.main_frame],
            key=lambda f: 0 if ("main" in f.name.lower() or "main" in f.url.lower()) else 1
        )
        
        for frame in sorted_frames:
            try:
                loc = frame.locator(selector)
                loc.first.click(timeout=3000, force=force)
                self.log.info(f"프레임 내부에서 요소를 찾아 클릭 성공: name={frame.name}, url={frame.url}")
                return True
            except Exception:
                continue
        return False

    def _click_in_page_frames(self, page, selector, timeout=3000):
        """특정 page의 메인 프레임 및 모든 자식 프레임에서 요소를 찾아 클릭합니다."""
        try:
            page.click(selector, timeout=1000)
            return True
        except Exception:
            pass
        for frame in page.frames:
            try:
                loc = frame.locator(selector)
                cnt = loc.count()
                for i in range(cnt):
                    el = loc.nth(i)
                    try:
                        el.click(timeout=timeout, force=True)
                        self.log.info(f"프레임 내부에서 요소 클릭 성공: selector={selector}")
                        return True
                    except Exception:
                        continue
            except Exception:
                continue
        return False

    def upload_to_kb_scan(self, stamped_pdf_path: str) -> bool:
        """4단계: 서명/스탬프 작업을 마친 동의서를 KB EDMS 스캔 시스템에 자동 전송합니다."""
        import solting_auto
        solting_auto.check_stop()

        self.log.info("4단계: KB스캔 자동 업로드/전송을 시작합니다.")
        import os
        import shutil
        
        filename = Path(stamped_pdf_path).name
        stem = Path(stamped_pdf_path).stem
        
        # 1) 파일 복사 (기본 문서 폴더 및 EDMS2 하위 폴더 둘 다 복사하여 접근 가능성 극대화)
        try:
            user_docs = Path(os.environ["USERPROFILE"]) / "Documents"
            
            # C:\Users\USER\Documents 에 직접 복사 (기본 "문서" 폴더)
            target_pdf_docs = user_docs / filename
            shutil.copy2(stamped_pdf_path, target_pdf_docs)
            self.log.info(f"EDMS 전송용 파일 복사 완료 (문서): {target_pdf_docs}")
            
            # C:\Users\USER\Documents\EDMS2 에 복사 (백업/서브폴더)
            edms_dir = user_docs / "EDMS2"
            edms_dir.mkdir(parents=True, exist_ok=True)
            target_pdf_path = edms_dir / filename
            shutil.copy2(stamped_pdf_path, target_pdf_path)
            self.log.info(f"EDMS 전송용 파일 복사 완료 (EDMS2): {target_pdf_path}")
        except Exception as copy_err:
            self.log.error(f"EDMS 파일 복사 중 예외 발생 (기본 경로로 업로드 시도): {copy_err}")
            target_pdf_path = Path(stamped_pdf_path)
            
        edms_page = None
        
        def is_edms_page(p) -> bool:
            try:
                p_url = p.url.lower()
                p_title = p.title().lower()
                
                # chrome-extension이나 blank는 제외
                if "chrome-extension://" in p_url or p_url == "about:blank":
                    return False
                
                # 1. edms 관련 키워드 매칭
                keywords = ["edms", "sso.do", "edmseus", "scan", "스캔", "이미지", "고객", "등록", "뷰어", "조회", "sso"]
                for kw in keywords:
                    if kw in p_url or kw in p_title:
                        return True
                        
                # 2. nsales가 아닌 kbinsure 도메인 관련 페이지
                if "kbinsure.co.kr" in p_url and "nsales" not in p_url:
                    return True
                    
                # 3. nsales.kbinsure.co.kr이 아닌 다른 외부/사내망 IP 페이지가 열렸다면 그것이 EDMS일 가능성이 높음
                if "nsales.kbinsure.co.kr" not in p_url and ("http://" in p_url or "https://" in p_url):
                    return True
            except Exception:
                pass
            return False

        try:
            # 2) 메인 창에서 KB스캔 메뉴 클릭 및 팝업 대기 (하이브리드 탐지 방식)
            # 선제 검색: 이미 열려있는 EDMS 창이 있는지 context.pages에서 검색
            for p in self.context.pages:
                if is_edms_page(p):
                    edms_page = p
                    self.log.info(f"이미 열려있는 EDMS 페이지를 context.pages에서 감지했습니다: {p.url}")
                    break

            if not edms_page:
                self.log.info("메인 메뉴에서 'KB스캔' 버튼 클릭 및 팝업 대기 중...")
                try:
                    with self.context.expect_page(timeout=10000) as pinfo:
                        success = self._click_in_frames("text=KB스캔", timeout=5000)
                        if not success:
                            for alt_sel in ["[id*='KB스캔']", "[id*='btnScan']", "[id*='btnPrtScan']", "a:has-text('KB스캔')"]:
                                if self._click_in_frames(alt_sel, timeout=2000):
                                    success = True
                                    break
                        if not success:
                            raise RegisterError("메인 메뉴에서 'KB스캔' 버튼을 찾을 수 없습니다.")
                    edms_page = pinfo.value
                    self.log.info(f"expect_page를 통해 새 EDMS 팝업 감지 완료: {edms_page.url}")
                except Exception as e:
                    self.log.warning(f"expect_page로 팝업 감지 실패(에러: {e}), context.pages에서 직접 재탐색을 시도합니다.")

            # 여전히 못 찾았다면 context.pages를 다시 스캔 (최대 8초 동안 0.5초 간격 폴링)
            if not edms_page:
                for attempt in range(16):
                    for p in self.context.pages:
                        if is_edms_page(p):
                            edms_page = p
                            self.log.info(f"context.pages 검색을 통해 EDMS 페이지 탐지 성공: {p.url}")
                            break
                    if edms_page:
                        break
                    time.sleep(0.5)

            if not edms_page:
                # 디버그용: 현재 열려있는 모든 페이지 목록 상세 출력
                self.log.error("--- EDMS 팝업 탐색 실패: 현재 열려있는 모든 페이지 목록 ---")
                for idx, p in enumerate(self.context.pages):
                    try:
                        self.log.error(f"  페이지 [{idx}]: URL='{p.url}', 제목='{p.title()}'")
                    except Exception as pe:
                        self.log.error(f"  페이지 [{idx}]: 정보 획득 에러 ({pe})")
                self.log.error("--------------------------------------------------")
                raise RegisterError("EDMS 팝업 창을 탐지하지 못했습니다.")

            # 안전한 로드 상태 대기 (타임아웃 시 경고만 출력하고 진행)
            try:
                edms_page.wait_for_load_state("load", timeout=8000)
            except Exception as load_err:
                self.log.warning(f"EDMS 로딩 상태 대기 중 경고 (계속 진행): {load_err}")

            self.log.info(f"EDMS 페이지 분석 진행: {edms_page.url}")
            
            # 3) EDMS 페이지 "고객" 아이콘 클릭
            self.log.info("EDMS '고객' 메뉴 클릭 시도...")
            success = self._click_in_page_frames(edms_page, "text=고객", timeout=3000)
            if not success:
                for alt_sel in ["[id*='고객']", "text='고객'", "div:has-text('고객')", "span:has-text('고객')"]:
                    if self._click_in_page_frames(edms_page, alt_sel, timeout=1500):
                        success = True
                        break
            if not success:
                raise RegisterError("EDMS 페이지에서 '고객' 메뉴를 찾을 수 없습니다.")
            time.sleep(1.5)
            
            # 4) "이미지추가" 버튼 클릭
            self.log.info("EDMS '이미지추가' 버튼 클릭 시도...")
            success = self._click_in_page_frames(edms_page, "text=이미지추가", timeout=3000)
            if not success:
                for alt_sel in ["text='이미지 추가'", "[id*='btnAdd']", "[id*='btnImgAdd']", "text=추가", "text=이미지", "span:has-text('이미지')"]:
                    if self._click_in_page_frames(edms_page, alt_sel, timeout=1500):
                        success = True
                        break
            if not success:
                raise RegisterError("EDMS 페이지에서 '이미지추가' 버튼을 찾을 수 없습니다.")
            time.sleep(1.5)
            
            # 5) "로컬PDF" 탭 클릭
            self.log.info("EDMS '로컬PDF' 탭 클릭 시도...")
            success = self._click_in_page_frames(edms_page, "text=로컬PDF", timeout=3000)
            if not success:
                for alt_sel in ["text='로컬 PDF'", "[id*='tabPdf']", "[id*='btnPdf']", "text=로컬", "text=PDF", "span:has-text('PDF')"]:
                    if self._click_in_page_frames(edms_page, alt_sel, timeout=1500):
                        success = True
                        break
            if not success:
                raise RegisterError("EDMS 페이지에서 '로컬PDF' 탭을 찾을 수 없습니다.")
            time.sleep(1.5)
            
            # 6) 트리에서 '문서' 및 'EDMS2' 폴더 차례대로 클릭
            self.log.info("파일 트리에서 '문서' (또는 'Documents') 폴더 선택 시도...")
            success_doc = self._click_in_page_frames(edms_page, "text=문서", timeout=2000)
            if not success_doc:
                success_doc = self._click_in_page_frames(edms_page, "text=Documents", timeout=2000)
            if not success_doc:
                self._click_in_page_frames(edms_page, "text=내 컴퓨터", timeout=1500)
                self._click_in_page_frames(edms_page, "text=내 PC", timeout=1500)
            time.sleep(0.5)
            
            self._click_in_page_frames(edms_page, "text=EDMS2", timeout=2000)
            time.sleep(1.0)
            
            # 7) 파일검색 입력창 찾기 및 파일식별명(stem) 입력 (확장자 숨김 환경 대응)
            self.log.info(f"파일검색 입력창 탐색 및 파일 식별명 '{stem}' 입력...")
            search_input = None
            for frame in edms_page.frames:
                try:
                    inputs = frame.locator("input[type='text'], input:not([type])")
                    cnt = inputs.count()
                    for i in range(cnt):
                        el = inputs.nth(i)
                        if el.is_visible():
                            el_id = el.get_attribute("id") or ""
                            el_class = el.get_attribute("class") or ""
                            if "search" in el_id.lower() or "file" in el_id.lower() or "find" in el_id.lower() or "search" in el_class.lower() or "nexaedit" in el_class.lower():
                                search_input = el
                                break
                    if search_input:
                        break
                except Exception:
                    continue
                    
            if not search_input:
                for frame in edms_page.frames:
                    try:
                        inputs = frame.locator("input[type='text'], input:not([type])")
                        cnt = inputs.count()
                        for i in range(cnt):
                            el = inputs.nth(i)
                            if el.is_visible():
                                search_input = el
                                break
                    except Exception:
                        continue
                        
            if search_input:
                search_input.click(force=True)
                search_input.evaluate("el => el.focus()")
                search_input.press("Control+A")
                search_input.press("Backspace")
                search_input.type(stem)
                time.sleep(0.5)
                search_input.press("Enter")
                self.log.info("파일 식별명 입력 및 Enter 입력 처리 완료")
            else:
                self.log.warning("파일검색 입력창을 특정하지 못해 목록 직접 매칭을 시도합니다.")
            time.sleep(1.5)

            # Playwright 방식의 제어가 불가능하므로 데스크톱 pyautogui 대안 자동화를 직접 실행합니다.
            self.log.warning("Playwright 방식의 제어가 불가능하므로 데스크톱 pyautogui 대안 자동화를 직접 실행합니다.")
            return self.batch_upload_via_win32([stamped_pdf_path])
        except Exception as e:
            self.log.error(f"Playwright 기반 KB스캔 자동 업로드 중 예외 발생: {e}")
            self.log.warning("데스크톱 pyautogui 대안 자동화를 개시합니다.")
            return self.batch_upload_via_win32([stamped_pdf_path])

    def batch_upload_via_win32(self, stamped_pdf_paths: list, progress_cb=None) -> bool:
        """Playwright 제어가 불가능한 경우(IE Mode, 보안 격리 등) pywinauto와 pyautogui를 이용한 데스크톱 수준 일괄 업로드를 수행합니다."""
        self.log.info(f"대안 구동: pywinauto/pyautogui 데스크톱 일괄 업로드({len(stamped_pdf_paths)}개)를 개시합니다.")
        import pywinauto
        from pywinauto import findwindows
        import pyautogui
        import win32gui
        import win32con
        import win32clipboard
        import time
        import json
        from pathlib import Path
        
        if not stamped_pdf_paths:
            self.log.warning("업로드할 파일 목록이 비어 있습니다.")
            return True

        # edms_config.json 로드
        import sys
        FROZEN = getattr(sys, "frozen", False)
        APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent.parent
        edms_cfg_path = APP_DIR / "edms_config.json"
        edms_cfg = {}
        if edms_cfg_path.exists():
            try:
                with open(edms_cfg_path, "r", encoding="utf-8") as f:
                    edms_cfg = json.load(f)
                self.log.info("edms_config.json 설정 로드 완료")
            except Exception as load_err:
                self.log.error(f"edms_config.json 로드 중 오류 발생: {load_err}")

        # 기본값 병합
        delays = {
            "dialog_open_wait": 1.0,
            "tab_click_wait": 1.0,
            "folder_expand_wait": 1.5,
            "search_wait": 2.0,
            "image_load_wait": 4.0,
            "select_all_wait": 1.0,
            "send_confirm_wait": 0.5,
            "success_alert_wait": 15.0
        }
        offsets = {
            "image_add_x": 1117,
            "image_add_y": 252,
            "select_all_x": 373,
            "select_all_y": 272,
            "send_x": 1185,
            "send_y": 252,
            "tab_local_pdf_x": 666,
            "tab_local_pdf_y": 38,
            "folder_docs_x": 578,
            "folder_docs_y": 92,
            "search_input_x": 883,
            "search_input_y": 345,
            "search_btn_x": 964,
            "search_btn_y": 345,
            "confirm_btn_x": 899,
            "confirm_btn_y": 617,
            "fallback_pop_send_x": 923,
            "fallback_pop_send_y": 692
        }
        ratios = {
            "pop_send_x": 0.693989,
            "pop_send_y": 0.873817
        }

        if "delays" in edms_cfg:
            delays.update(edms_cfg["delays"])
        if "offsets" in edms_cfg:
            offsets.update(edms_cfg["offsets"])
        if "ratios" in edms_cfg:
            ratios.update(edms_cfg["ratios"])

        # 0) 파일 복사 (기본 문서 폴더 및 EDMS2 하위 폴더 둘 다 복사하여 접근 가능성 극대화)
        import os
        import shutil
        try:
            user_docs = Path(os.environ["USERPROFILE"]) / "Documents"
            edms_dir = user_docs / "EDMS2"
            edms_dir.mkdir(parents=True, exist_ok=True)
            for pdf_path_str in stamped_pdf_paths:
                pdf_path = Path(pdf_path_str)
                if pdf_path.exists():
                    filename = pdf_path.name
                    # C:\Users\USER\Documents 에 직접 복사 (기본 "문서" 폴더)
                    target_pdf_docs = user_docs / filename
                    shutil.copy2(pdf_path, target_pdf_docs)
                    self.log.info(f"EDMS 전송용 파일 복사 완료 (문서): {target_pdf_docs}")
                    
                    # C:\Users\USER\Documents\EDMS2 에 복사 (백업/서브폴더)
                    target_pdf_path = edms_dir / filename
                    shutil.copy2(pdf_path, target_pdf_path)
                    self.log.info(f"EDMS 전송용 파일 복사 완료 (EDMS2): {target_pdf_path}")
                else:
                    self.log.warning(f"복사할 원본 파일이 존재하지 않습니다: {pdf_path_str}")
        except Exception as copy_err:
            self.log.error(f"EDMS 일괄 전송을 위한 파일 복사 중 예외 발생: {copy_err}")
            
        def set_clipboard(text):
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            win32clipboard.CloseClipboard()
            
        try:
            # 1) EDMS 창 연결 및 포커싱
            elements = findwindows.find_elements(title_re=".*EDMS.*")
            if not elements:
                raise RegisterError("EDMS 팝업 창을 찾을 수 없습니다.")
            
            hwnd = elements[0].handle
            self.log.info(f"EDMS 창 감지됨 (Handle: {hwnd})")
            
            # 창 최대화 및 포커싱
            try:
                win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
                time.sleep(0.5)
                win32gui.SetForegroundWindow(hwnd)
                time.sleep(0.5)
            except Exception as f_err:
                self.log.warning(f"창 최대화/활성화 실패: {f_err}")
                
            win_rect = win32gui.GetWindowRect(hwnd)
            win_x, win_y = win_rect[0], win_rect[1]
            self.log.info(f"EDMS 창 좌표: ({win_x}, {win_y})")
            
            # 2) Solting App 터미널 창 최소화 (클릭 방해 방지)
            terminals = findwindows.find_elements(title_re=".*Solting App.*")
            if not terminals:
                terminals = findwindows.find_elements(title_re=".*Analyzing and Running.*")
            for t in terminals:
                try:
                    win32gui.ShowWindow(t.handle, win32con.SW_MINIMIZE)
                except:
                    pass
            time.sleep(1.0)
            
            # 3) safe area 클릭하여 포커스 강제화 (수동 '고객' 메뉴 진입 대응)
            pyautogui.click(200, 400)
            time.sleep(1.0)
            
            success_count = 0
            for idx, pdf_path_str in enumerate(stamped_pdf_paths):
                # 루프 시작 시 중단 감지
                if getattr(self, "stop_requested", False):
                    self.log.warning("사용자에 의해 매크로 중단이 요청되었습니다. 업로드를 조기 중단합니다.")
                    if progress_cb:
                        progress_cb(idx, len(stamped_pdf_paths), "중단됨")
                    break

                pdf_path = Path(pdf_path_str)
                stem = pdf_path.stem
                if progress_cb:
                    progress_cb(idx, len(stamped_pdf_paths), f"'{pdf_path.name}' 이미지 추가 진행 중...")
                self.log.info(f"[{idx+1}/{len(stamped_pdf_paths)}] 파일 이미지 추가 및 전송 중: '{pdf_path.name}'")
                
                try:
                    # 매 루프 시작 시 EDMS 창 포커싱 강제화
                    try:
                        win32gui.SetForegroundWindow(hwnd)
                        time.sleep(0.5)
                    except Exception as focus_err:
                        self.log.warning(f"루프 내 창 활성화 실패: {focus_err}")
                        
                    # 4) "이미지추가" 버튼 클릭
                    if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                    x_add = win_x + int(offsets["image_add_x"])
                    y_add = win_y + int(offsets["image_add_y"])
                    self.log.info(f"UIA: '이미지추가' 버튼 클릭 시도 -> ({x_add}, {y_add})")
                    pyautogui.moveTo(x_add, y_add, duration=0.5)
                    pyautogui.mouseDown()
                    time.sleep(0.2)
                    pyautogui.mouseUp()
                    
                    # 5) "이미지 추가" 다이얼로그 팝업 대기 및 핸들 획득
                    self.log.info("이미지 추가 다이얼로그 대기 (최대 5초)...")
                    dialogs = []
                    for _ in range(10):
                        if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                        dialogs = findwindows.find_elements(title_re=".*이미지.*")
                        if dialogs:
                            break
                        time.sleep(0.5)
                    if not dialogs:
                        raise RegisterError("'이미지 추가' 다이얼로그가 열리지 않았습니다.")
                    
                    dlg_hwnd = dialogs[0].handle
                    dlg_rect = win32gui.GetWindowRect(dlg_hwnd)
                    dx, dy = dlg_rect[0], dlg_rect[1]
                    self.log.info(f"다이얼로그 좌표: dx={dx}, dy={dy}")
                    
                    time.sleep(delays["dialog_open_wait"])

                    # 6) "로컬PDF" 탭 클릭
                    if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                    self.log.info("UIA: '로컬PDF' 탭 클릭 시도...")
                    pyautogui.moveTo(dx + int(offsets["tab_local_pdf_x"]), dy + int(offsets["tab_local_pdf_y"]), duration=0.5)
                    pyautogui.mouseDown()
                    time.sleep(0.2)
                    pyautogui.mouseUp()
                    time.sleep(delays["tab_click_wait"])
                    
                    # 7) "문서" 폴더 더블클릭하여 하위 폴더 확장
                    if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                    self.log.info("UIA: 파일 트리 '문서' 폴더 확장...")
                    pyautogui.moveTo(dx + int(offsets["folder_docs_x"]), dy + int(offsets["folder_docs_y"]), duration=0.5)
                    pyautogui.doubleClick()
                    time.sleep(delays["folder_expand_wait"])
                    
                    # 8) 파일검색 입력창 클릭 및 파일명 입력
                    if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                    self.log.info(f"UIA: 파일검색 창에 '{stem}' 입력...")
                    pyautogui.moveTo(dx + int(offsets["search_input_x"]), dy + int(offsets["search_input_y"]), duration=0.5)
                    pyautogui.mouseDown()
                    time.sleep(0.2)
                    pyautogui.mouseUp()
                    time.sleep(0.2)
                    pyautogui.hotkey('ctrl', 'a')
                    pyautogui.press('backspace')
                    time.sleep(0.2)
                    
                    if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                    set_clipboard(stem)
                    pyautogui.hotkey('ctrl', 'v')
                    time.sleep(0.5)
                    
                    # 9) 돋보기(검색) 클릭
                    if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                    self.log.info("UIA: 파일검색 돋보기 클릭...")
                    pyautogui.moveTo(dx + int(offsets["search_btn_x"]), dy + int(offsets["search_btn_y"]), duration=0.5)
                    pyautogui.mouseDown()
                    time.sleep(0.2)
                    pyautogui.mouseUp()
                    time.sleep(delays["search_wait"])
                    
                    # 10) "확인" 버튼 클릭하여 이미지 로드
                    if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                    self.log.info("UIA: '확인' 버튼 클릭...")
                    pyautogui.moveTo(dx + int(offsets["confirm_btn_x"]), dy + int(offsets["confirm_btn_y"]), duration=0.5)
                    pyautogui.mouseDown()
                    time.sleep(0.25)
                    pyautogui.mouseUp()
                    
                    self.log.info(f"UIA: '이미지 추가' 완료. 이미지 로드 대기 ({delays['image_load_wait']}초)...")
                    time.sleep(delays["image_load_wait"])
                    
                    # 11) 전체선택 버튼 클릭
                    if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                    x_select = win_x + int(offsets["select_all_x"])
                    y_select = win_y + int(offsets["select_all_y"])
                    self.log.info(f"UIA: '전체선택' 버튼 클릭 시도 -> ({x_select}, {y_select})")
                    pyautogui.moveTo(x_select, y_select, duration=0.5)
                    pyautogui.mouseDown()
                    time.sleep(0.2)
                    pyautogui.mouseUp()
                    time.sleep(delays["select_all_wait"])
                    
                    # 12) 전송 (메인) 버튼 클릭
                    if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                    x_send = win_x + int(offsets["send_x"])
                    y_send = win_y + int(offsets["send_y"])
                    self.log.info(f"UIA: 메인 화면 '전송' 버튼 클릭 시도 -> ({x_send}, {y_send})")
                    pyautogui.moveTo(x_send, y_send, duration=0.5)
                    pyautogui.mouseDown()
                    time.sleep(0.2)
                    pyautogui.mouseUp()
                    
                    # 13) 전송 확인 팝업창 대기 및 핸들 획득
                    if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                    self.log.info(f"UIA: '전송 확인' 팝업창 대기 ({delays['send_confirm_wait']}초)...")
                    time.sleep(delays["send_confirm_wait"])
                    pop_hwnd = None
                    for _ in range(10):
                        if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                        popups = findwindows.find_elements(title="전송 확인")
                        if not popups:
                            popups = findwindows.find_elements(title_re=".*전송.*")
                        popups = [p for p in popups if p.handle != hwnd and ('dlg_hwnd' not in locals() or p.handle != dlg_hwnd)]
                        if popups:
                            pop_hwnd = popups[0].handle
                            break
                        time.sleep(0.5)
                        
                    if pop_hwnd:
                        pop_rect = win32gui.GetWindowRect(pop_hwnd)
                        p_dx, p_dy = pop_rect[0], pop_rect[1]
                        p_w = pop_rect[2] - pop_rect[0]
                        p_h = pop_rect[3] - pop_rect[1]
                        self.log.info(f"UIA: '전송 확인' 팝업 감지됨 (Handle: {pop_hwnd}, 크기: {p_w}x{p_h}, 좌표: {p_dx}, {p_dy})")
                        
                        if "pop_send_btn_x" in offsets and "pop_send_btn_y" in offsets:
                            x_pop_send = p_dx + int(offsets["pop_send_btn_x"])
                            y_pop_send = p_dy + int(offsets["pop_send_btn_y"])
                            self.log.info(f"UIA: 팝업창 내 '전송' 확인 버튼 클릭 시도 (직접 오프셋) -> ({x_pop_send}, {y_pop_send})")
                        else:
                            x_pop_send = p_dx + int(p_w * float(ratios["pop_send_x"]))
                            y_pop_send = p_dy + int(p_h * float(ratios["pop_send_y"]))
                            self.log.info(f"UIA: 팝업창 내 '전송' 확인 버튼 클릭 시도 (비율 기반) -> ({x_pop_send}, {y_pop_send})")
                        
                        try:
                            win32gui.SetForegroundWindow(pop_hwnd)
                            time.sleep(0.3)
                        except Exception as focus_err:
                            self.log.warning(f"팝업창 포커싱 실패: {focus_err}")
                            
                        if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                        pyautogui.moveTo(x_pop_send, y_pop_send, duration=0.5)
                        pyautogui.mouseDown()
                        time.sleep(0.25)
                        pyautogui.mouseUp()
                    else:
                        self.log.warning("UIA: '전송 확인' 팝업창을 찾을 수 없어, 기존의 추정 좌표 클릭 시도")
                        x_pop_send = win_x + int(offsets["fallback_pop_send_x"])
                        y_pop_send = win_y + int(offsets["fallback_pop_send_y"])
                        if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                        pyautogui.moveTo(x_pop_send, y_pop_send, duration=0.5)
                        pyautogui.mouseDown()
                        time.sleep(0.25)
                        pyautogui.mouseUp()
                        
                    self.log.info(f"UIA: 전송 완료 및 성공 알림창 대기 (최대 {delays['success_alert_wait']}초)...")
                    alert_found = False
                    alert_titles = ["알림", "정보", "확인", "Message", "EDMS"]
                    
                    scan_loops = max(1, int(delays["success_alert_wait"] * 2))
                    for _ in range(scan_loops):
                        if getattr(self, "stop_requested", False): raise RegisterError("사용자 중단 요청")
                        for title_name in alert_titles:
                            try:
                                alerts = findwindows.find_elements(title=title_name)
                                if not alerts:
                                    alerts = findwindows.find_elements(title_re=f".*{title_name}.*")
                                alerts = [a for a in alerts if a.handle != hwnd and ('dlg_hwnd' not in locals() or a.handle != dlg_hwnd) and ('pop_hwnd' not in locals() or a.handle != pop_hwnd)]
                                if alerts:
                                    alert_hwnd = alerts[0].handle
                                    self.log.info(f"UIA: 성공 알림창 감지됨: '{alerts[0].name}' (Handle: {alert_hwnd})")
                                    try:
                                        win32gui.SetForegroundWindow(alert_hwnd)
                                        time.sleep(0.3)
                                    except:
                                        pass
                                    pyautogui.press('enter')
                                    time.sleep(0.5)
                                    alert_found = True
                                    break
                            except Exception as alert_err:
                                pass
                        if alert_found:
                            break
                        time.sleep(0.5)
                        
                    if not alert_found:
                        self.log.info("UIA: 지정된 대기 시간 동안 명시적인 성공 알림창이 감지되지 않았습니다. 안전 예방을 위한 Enter 전송.")
                        pyautogui.press('enter')
                        time.sleep(0.5)
                    
                    success_count += 1
                    if progress_cb:
                        progress_cb(idx + 1, len(stamped_pdf_paths), f"'{pdf_path.name}' 전송 완료")
                        
                except Exception as file_err:
                    # 사용자 중단 요청인 경우 루프 강제 탈출
                    if getattr(self, "stop_requested", False) or "중단" in str(file_err):
                        self.log.warning("사용자 중단 감지: 일괄 업로드 루프를 즉시 종료합니다.")
                        if progress_cb:
                            progress_cb(idx, len(stamped_pdf_paths), "중단됨")
                        break

                    self.log.error(f"파일 '{pdf_path.name}' 처리 중 오류 발생: {file_err}")
                    try:
                        pyautogui.press('enter')
                        time.sleep(0.5)
                        pyautogui.press('esc')
                        time.sleep(1.0)
                    except:
                        pass
                    if progress_cb:
                        progress_cb(idx + 1, len(stamped_pdf_paths), f"'{pdf_path.name}' 실패: {file_err}")
                
            if success_count == 0:
                self.log.warning("전송에 성공한 파일이 전혀 없습니다.")
                return False
            
            # 14) EDMS 창 닫기
            try:
                self.log.info("UIA: EDMS 창 닫기 시도 (WM_CLOSE)...")
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                time.sleep(1.0)
            except Exception as close_err:
                self.log.warning(f"UIA: WM_CLOSE 전송 실패, Alt+F4 시도: {close_err}")
                try:
                    pyautogui.hotkey('alt', 'f4')
                except Exception as f4_err:
                    self.log.error(f"UIA: Alt+F4 실패: {f4_err}")
            
            # 15) Solting App 터미널 창 복원
            for t in terminals:
                try:
                    win32gui.ShowWindow(t.handle, win32con.SW_RESTORE)
                except:
                    pass
            
            return True
            
        except Exception as e:
            self.log.error(f"UIA 대형 일괄 업로드 자동화 중 에러 발생: {e}")
            try:
                terminals = findwindows.find_elements(title_re=".*Solting.*")
                for t in terminals:
                    win32gui.ShowWindow(t.handle, win32con.SW_RESTORE)
            except:
                pass
            return False
