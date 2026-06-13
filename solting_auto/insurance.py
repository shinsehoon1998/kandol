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
from .reporter import SUCCESS, FAIL, SKIP


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
        
        self.last_dialog_alert = None
        self.last_skip_reason = "최근 등록 이력 존재(2개월 이내)"
        def handle_dialog(dialog_obj):
            msg = dialog_obj.message
            self.log.info(f"[알림 감지] 브라우저 경고창 처리 시작: {msg}")
            self.last_dialog_alert = msg
            if "재입력" in msg or "당사에 입력된" in msg or "최근" in msg:
                self.log.info("[알림 감지] 최근 입력 이력 중복창이므로 dismiss(아니오/취소) 처리합니다.")
                dialog_obj.dismiss()
            else:
                self.log.info("[알림 감지] 일반 경고/이미 서면 동의 알림이므로 accept(확인) 처리합니다.")
                dialog_obj.accept()
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
            self.last_skip_reason = "최근 등록 이력 존재(2개월 이내)"  # 초기 상태 리셋
            self.last_dialog_alert = None  # 브라우저 얼럿 상태 리셋
            # 입력 (주민번호 먼저 입력 후 고객명 기입으로 순서 정상화)
            self._fill_smart(dialog, self.sel["consent"].get("jumin"), self._fmt_jumin(jumin), "주민번호")
            self._fill_smart(dialog, self.sel["consent"].get("name"), str(name), "이름")
            
            # 입력 후 2.0초 이내에 중복 팝업이 뜨는지 감지
            is_dup = False
            for _ in range(10):
                solting_auto.check_stop()
                if self._check_duplicate_popup(dialog):
                    is_dup = True
                    break
                time.sleep(0.2)
                
            if is_dup:
                reason = getattr(self, "last_skip_reason", "최근 등록 이력 존재(2개월 이내)")
                self.log.info(f"[{name}] 중복 경고 감지: {reason}. 등록을 건너뜁니다.")
                raise DuplicateCustomerError(reason)

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
                
                # 셀렉터 포맷팅 (KB손보 다중입력 ID 규칙 대응)
                if self.sel["consent"].get("multi_jumin_format") == "[id*='iptCustIdno_{i}']":
                    if i == 0:
                        jumin_sel = "[id$='iptCustIdno']"
                    else:
                        jumin_sel = f"[id$='iptCustIdno_{i+1:02d}']"
                else:
                    jumin_sel = self.sel["consent"].get("multi_jumin_format", "[id*='iptCustIdno_{i}']").replace("{i}", str(i))
                    
                if self.sel["consent"].get("multi_name_format") == "[id*='iptCustNm_{i}']":
                    if i == 0:
                        name_sel = "[id$='iptCustNm']"
                    else:
                        name_sel = f"[id$='iptCustNm_{i+1:02d}']"
                else:
                    name_sel = self.sel["consent"].get("multi_name_format", "[id*='iptCustNm_{i}']").replace("{i}", str(i))
                
                self.last_skip_reason = "최근 등록 이력 존재(2개월 이내)"  # 초기 상태 리셋
                self.last_dialog_alert = None  # 브라우저 얼럿 상태 리셋
                self.log.info(f"[{rec['name']}] 다중입력 슬롯 {i}번에 입력 중... jumin_sel={jumin_sel}, name_sel={name_sel}")
                
                # 입력 실행
                self._fill_smart(dialog, jumin_sel, self._fmt_jumin(rec['jumin']), f"주민번호 {i}")
                self._fill_smart(dialog, name_sel, str(rec['name']), f"이름 {i}")
                
                # 중복 팝업 실시간 감지 (2초 대기하며 감지)
                is_dup = False
                for _ in range(10):
                    solting_auto.check_stop()
                    if self._check_duplicate_popup(dialog):
                        is_dup = True
                        break
                    time.sleep(0.2)
                    
                if is_dup:
                    reason = getattr(self, "last_skip_reason", "최근 등록 이력 존재(2개월 이내)")
                    self.log.info(f"[{rec['name']}] 중복 경고 모달 감지됨. 이 레코드는 건너뜁니다. 사유: {reason}")
                    # 입력했던 슬롯 비우기
                    self._clear_input(dialog, name_sel)
                    self._clear_input(dialog, jumin_sel)
                    results.append({
                        "row_no": rec["row_no"],
                        "jumin": rec["jumin"],
                        "name": rec["name"],
                        "phone": rec["phone"],
                        "status": SKIP,
                        "reason": reason,
                        "pdf_path": ""
                    })
                else:
                    # 체크박스 선택 (가입설계 & 선택동의 모두 체크)
                    idx = i + 1
                    sel1 = f"[id$='group2_checkbox{idx}_input_0']:not([id*='group2_group2_'])"
                    sel2 = f"[id$='group2_group2_checkbox{idx}_input_0']"
                    
                    for sel in [sel1, sel2]:
                        try:
                            # 프레임들 중 해당 셀렉터가 존재하는 프레임을 찾아서 JS click 실행 (visibility 제약 우회)
                            frames_to_search = [self.page] + list(self.page.frames)
                            for f in frames_to_search:
                                has_el = f.evaluate(f"() => document.querySelector(\"{sel}\") !== null")
                                if has_el:
                                    is_checked = f.evaluate(f"() => {{ return document.querySelector(\"{sel}\").checked; }}")
                                    if not is_checked:
                                        f.evaluate(f"() => {{ document.querySelector(\"{sel}\").click(); }}")
                                        self.log.info(f"슬롯 {i}번 체크박스 선택 완료 ({sel})")
                                    break
                        except Exception as e:
                            self.log.warning(f"슬롯 {i}번 체크박스 선택 오류 ({sel}): {e}")
                    
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
            # ※ 다중입력 '출력'은 OZ 데스크톱 뷰어가 아니라 브라우저로 PDF 가 직접 열린다.
            #    따라서 단일모드(OZ)와 달리 네트워크 응답/다운로드로 PDF 를 캡처해 지정 폴더에 저장한다.
            actual_format = "PDF"
            batch_id = int(time.time())
            temp_dest = self.pdf_dir / f"batch_temp_{batch_id}.pdf"

            self.log.info(f"등록 성공 고객 {len(registered_records)}명 일괄 출력(브라우저 PDF) 시작")

            print_btn = self.sel["consent"].get("print_btn")
            out_timeout = float(self.ins.get("oz", {}).get("open_timeout_sec", 20))

            # 출력 시점 '이미 서면 동의를 받은 고객' 차단 알림이 뜨면, 해당 고객을 배치에서
            # 제외(행 비우기 + 체크해제 + SKIP)하고 나머지 고객으로 재출력한다. (끊김 없이 진행)
            path = None
            max_removals = len(registered_records) + 1
            for _ in range(max_removals):
                if not registered_records:
                    self.log.info("서면동의 완료 등으로 출력 가능한 고객이 모두 제외되었습니다. 출력을 종료합니다.")
                    return results

                self.log.info(f"등록 고객 {len(registered_records)}명 일괄 출력(PDF) 시도")
                state, birth6 = self._capture_print_pdf_or_block(dialog, print_btn, temp_dest, timeout=out_timeout)

                if state == "blocked":
                    # 출력 차단 통지는 (입력시점)생년월일 또는 (출력시점)'N번째 고객' 순번으로 온다.
                    block_msg = getattr(self, "last_block_msg", "") or ""
                    pos = self._extract_position(block_msg)
                    removed = None
                    if birth6:
                        for k, rrec in enumerate(registered_records):
                            if normalize_digits(rrec["jumin"])[:6] == birth6:
                                removed = k
                                break
                    if removed is None and 1 <= pos <= len(registered_records):
                        removed = pos - 1  # registered_records 는 체크(출력)된 순서
                    if removed is None:
                        removed = 0  # 최후: 첫 등록 제거(무한루프 방지)

                    gone = registered_records.pop(removed)
                    self._clear_and_uncheck_row(gone["jumin"])
                    for r_item in results:
                        if r_item["row_no"] == gone["row_no"]:
                            r_item["status"] = SKIP
                            r_item["reason"] = "이미 서면 동의를 받은 고객(2개월 이내)"
                            r_item["pdf_path"] = ""
                    self.log.info(f"[{gone['name']}] 출력 차단(순번={pos}, 생일={birth6}) → 배치에서 제외 후 재출력합니다.")
                    self.last_block_msg = ""
                    time.sleep(0.5)
                    continue  # 나머지 고객으로 재출력

                if state == "timeout":
                    raise RetryableError("출력 후 PDF/차단알림이 모두 감지되지 않았습니다(타임아웃).")

                # state == "pdf": temp_dest 에 PDF 저장 완료
                path = str(temp_dest)
                break

            if path is None:
                self.log.warning("일괄 출력을 완료하지 못했습니다(출력 가능 고객 없음).")
                return results

            # 5) 생성된 파일 검증 및 개별 분할 저장 (다중출력은 항상 PDF → fitz 분할)
            if str(path).lower().endswith(".png"):
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

    # ── 출력 시점 '이미 서면 동의를 받은 고객' 차단 알림 처리 ──────────────
    def _is_consent_block_alert(self, msg) -> bool:
        """'이미 서면 동의를 받은 고객이라 동의서를 출력할 수 없다'는 출력 차단 알림인지 판별.
        (입력 시점의 '재입력하시겠습니까?' 와는 다른, 출력 버튼 클릭 시 뜨는 네이티브 alert)
        """
        if not msg:
            return False
        # WebSquare 텍스트의 비분리공백(\xa0) 등에 강하도록 공백 제거 후 비교
        compact = re.sub(r"\s", "", msg)
        keys = ["이미 서면 동의", "서면 동의를 받은", "출력하실 수 없습니다", "2개월 이후"]
        return any(re.sub(r"\s", "", k) in compact for k in keys)

    def _extract_birthdate(self, msg) -> str:
        """알림 문구에서 생년월일 6자리를 추출. 예) '600303 생년월일 고객은...' -> '600303'. 없으면 ''"""
        if not msg:
            return ""
        m = re.search(r"(\d{6})\s*생년월일", msg)
        if m:
            return m.group(1)
        # 보조: 메시지 내 6자리 숫자 폴백
        m2 = re.search(r"\b(\d{6})\b", msg)
        return m2.group(1) if m2 else ""

    def _extract_position(self, msg) -> int:
        """출력 시점 차단 알림의 'N번째 고객은...' 에서 순번 N 추출. 없으면 0.
        (다중입력 출력 차단은 생년월일이 아닌 순번으로 통지된다)"""
        if not msg:
            return 0
        m = re.search(r"(\d+)\s*번째", msg)
        return int(m.group(1)) if m else 0

    def _capture_print_pdf_or_block(self, dialog, print_btn, dest_pdf, timeout=20.0):
        """다중입력 '출력': OZ 뷰어가 아니라 브라우저로 PDF 가 직접 열린다.
        출력 클릭 후 (a)서면동의 차단 모달 또는 (b)PDF(네트워크 응답/다운로드)를 감시·캡처한다.
        PDF 는 dest_pdf 에 저장. 반환: ('blocked', birth6) | ('pdf', '') | ('timeout', '').
        """
        import solting_auto
        pdf_bodies = []
        dl_holder = []

        def on_resp(resp):
            try:
                ct = (resp.headers or {}).get("content-type", "").lower()
                url = (resp.url or "")
                base = url.split("?")[0].lower()
                looks_pdf = ("application/pdf" in ct or "octet-stream" in ct
                             or base.endswith(".pdf")
                             or any(k in url.lower() for k in self.url_keywords))
                if looks_pdf:
                    body = resp.body()
                    if body and body[:4] == b"%PDF":
                        pdf_bodies.append(body)
            except Exception:
                pass

        def attach(pg):
            try:
                pg.on("response", on_resp)
                pg.on("download", lambda d: dl_holder.append(d))
            except Exception:
                pass

        for pg in self.context.pages:
            attach(pg)
        self.context.on("page", attach)

        self.last_dialog_alert = None
        try:
            self._click_print_btn(dialog, print_btn)
        except Exception as e:
            try: self.context.remove_listener("page", attach)
            except Exception: pass
            raise RetryableError(f"'출력' 버튼 클릭 실패: {e}")

        result = ("timeout", "")
        deadline = time.time() + timeout
        while time.time() < deadline:
            solting_auto.check_stop()
            try:
                h, k, b = self._detect_block_modal(dialog)
            except Exception:
                h, k, b = (False, "", "")
            if h and k == "consent_block":
                result = ("blocked", b)
                break
            if pdf_bodies:
                try:
                    dest_pdf.parent.mkdir(parents=True, exist_ok=True)
                    best = max(pdf_bodies, key=len)
                    dest_pdf.write_bytes(best)
                    self.log.info(f"[다중출력] PDF 네트워크 응답 캡처 저장: {dest_pdf.name} ({len(best)} bytes)")
                    result = ("pdf", "")
                    break
                except Exception as e:
                    self.log.warning(f"PDF 응답 저장 실패: {e}")
            if dl_holder:
                try:
                    dl_holder[0].save_as(str(dest_pdf))
                    self.log.info(f"[다중출력] PDF 다운로드 캡처 저장: {dest_pdf.name}")
                    result = ("pdf", "")
                    break
                except Exception as e:
                    self.log.warning(f"PDF 다운로드 저장 실패: {e}")
            time.sleep(0.4)

        try: self.context.remove_listener("page", attach)
        except Exception: pass
        return result

    def _clear_and_uncheck_row(self, jumin_digits: str) -> bool:
        """다중입력 그리드에서 주민번호가 jumin_digits 와 일치하는 행의 성명/주민번호를 비우고
        해당 행의 체크박스(가입설계/선택동의)도 해제한다. (출력 차단 고객 배치 제외용)"""
        from .validators import normalize_digits
        jd = normalize_digits(jumin_digits)
        if not jd:
            return False
        frames = [self.page] + list(self.page.frames)
        for f in frames:
            try:
                els = f.query_selector_all("input[id*='iptCustIdno']")
            except Exception:
                continue
            for el in els:
                try:
                    raw = normalize_digits(el.input_value() or "")
                except Exception:
                    continue
                if raw and raw == jd:
                    el_id = el.get_attribute("id") or ""
                    try:
                        el.fill("")
                        el.evaluate("e=>{e.dispatchEvent(new Event('change',{bubbles:true}));e.dispatchEvent(new Event('input',{bubbles:true}));}")
                    except Exception:
                        pass
                    # 같은 행 성명칸
                    if "iptCustIdno" in el_id:
                        nid = el_id.replace("iptCustIdno", "iptCustNm")
                        try:
                            nel = f.query_selector(f"[id='{nid}']")
                            if nel:
                                nel.fill("")
                        except Exception:
                            pass
                    # 행 인덱스 추정 → 체크박스 해제
                    m = re.search(r"iptCustIdno_(\d{2})$", el_id)
                    idx = int(m.group(1)) if m else 1
                    for csel in (f"[id$='group2_checkbox{idx}_input_0']:not([id*='group2_group2_'])",
                                 f"[id$='group2_group2_checkbox{idx}_input_0']"):
                        try:
                            for cf in frames:
                                if cf.evaluate(f'()=>document.querySelector("{csel}")!==null'):
                                    cf.evaluate(f'()=>{{const e=document.querySelector("{csel}"); if(e&&e.checked)e.click();}}')
                                    break
                        except Exception:
                            pass
                    self.log.info(f"[출력차단] 주민 {jd[:6]}*** 행 비움+체크해제 (id={el_id}, idx={idx})")
                    return True
        self.log.warning(f"[출력차단] 주민 {jd[:6]}*** 행을 그리드에서 찾지 못했습니다.")
        return False

    def _wait_consent_block(self, timeout: float = 3.0, dialog=None):
        """출력 버튼 클릭 직후, '이미 서면 동의를 받은 고객' 출력 차단 알림이 뜨는지 timeout 동안 폴링.
        KB 알림은 WebSquare DOM 모달이므로 네이티브 alert(last_dialog_alert) 와 DOM 모달을 모두 확인한다.
        감지 시 (True, 생년월일str), 미감지 시 (False, "").
        """
        import solting_auto
        deadline = time.time() + timeout
        while time.time() < deadline:
            solting_auto.check_stop()
            # 1) 네이티브 alert (혹시 native 인 경우)
            msg = getattr(self, "last_dialog_alert", None)
            if msg:
                if self._is_consent_block_alert(msg):
                    self.last_dialog_alert = None
                    return True, self._extract_birthdate(msg)
                self.last_dialog_alert = None
            # 2) WebSquare DOM 모달
            try:
                handled, kind, birth6 = self._detect_block_modal(dialog)
            except Exception as e:
                self.log.debug(f"[중복모달] 출력시점 감지 예외(무시): {e}")
                handled, kind, birth6 = (False, "", "")
            if handled and kind == "consent_block":
                return True, birth6
            time.sleep(0.2)
        return False, ""

    def _await_output_or_block(self, dialog, timeout: float = 20.0):
        """출력 버튼 클릭 후, '이미 서면 동의' 차단 모달과 OZ 리포트 뷰어 등장을 동시에 감시한다.
        (입력 시점에 누락된 서면동의 고객이 출력 시점에 늦게 차단 모달을 띄워도 안전하게 잡기 위함)
        반환: ('blocked', birth6) | ('oz', '') | ('timeout', '')
        """
        import solting_auto
        from . import oz_viewer
        oz_cfg = self.ins.get("oz", {})
        deadline = time.time() + timeout
        while time.time() < deadline:
            solting_auto.check_stop()
            # 1) 차단 모달 우선 확인(있으면 확인 클릭하며 닫음)
            try:
                handled, kind, birth6 = self._detect_block_modal(dialog)
            except Exception:
                handled, kind, birth6 = (False, "", "")
            if handled and kind == "consent_block":
                return "blocked", birth6
            # 2) OZ 뷰어가 떴는지 확인 → 떴으면 정상 출력 진행
            try:
                if oz_viewer.oz_window_exists(oz_cfg):
                    return "oz", ""
            except Exception:
                pass
            time.sleep(0.4)
        return "timeout", ""

    def _modal_search_targets(self, dialog=None):
        """모달을 탐색할 프레임/페이지 목록(중복 제거). dialog(팝업 페이지)도 포함."""
        targets = []
        if dialog is not None:
            try:
                if dialog not in targets:
                    targets.append(dialog)
                if hasattr(dialog, "frames"):
                    for fr in dialog.frames:
                        if fr not in targets:
                            targets.append(fr)
            except Exception:
                pass
        try:
            if self.page not in targets:
                targets.append(self.page)
            for fr in self.page.frames:
                if fr not in targets:
                    targets.append(fr)
        except Exception:
            pass
        return targets

    def _click_modal_button(self, frame, modal_el, btn_texts) -> bool:
        """모달 내부 우선 → 프레임 전역 순으로 지정 텍스트 버튼을 강건하게 클릭(WebSquare 대응)."""
        # KB(WebSquare) confpop 의 확정 버튼 id 접미사 (실 DOM 확인됨)
        ws_id = {"확인": "btn_confirm", "아니오": "btn_no", "예": "btn_yes", "취소": "btn_cancel"}
        for txt in btn_texts:
            candidates = []
            if txt in ws_id:
                candidates.append(f"input[id$='{ws_id[txt]}']")
            candidates += [
                f"button:has-text('{txt}')", f"a:has-text('{txt}')",
                f"input[type='button'][value='{txt}']", f"input[value='{txt}']",
                f".w2trigger:has-text('{txt}')", f".w2textbox:has-text('{txt}')",
                f"[class*='btn']:has-text('{txt}')", f"[class*='trigger']:has-text('{txt}')",
                f"text='{txt}'", f"span:has-text('{txt}')", f"div:has-text('{txt}')",
            ]
            for scope in (modal_el, frame):
                if scope is None:
                    continue
                for cs in candidates:
                    try:
                        b = scope.locator(cs)
                        if b.count() > 0 and b.first.is_visible():
                            try:
                                b.first.click(force=True, timeout=2000)
                            except Exception:
                                b.first.evaluate("e => e.click()")
                            return True
                    except Exception:
                        continue
        return False

    def _detect_block_modal(self, dialog=None):
        """모든 프레임에서 WebSquare '알림' DOM 모달(서면동의 완료 / 최근입력 중복)을 찾아
        분류 후 적절한 버튼(확인 / 아니오)을 눌러 닫는다.
        반환: (handled: bool, kind: str, birth6: str)  kind ∈ {'consent_block','recent_input',''}
        """
        # KB(WebSquare) 알림/확인 팝업은 .w2floatingLayer.confpop 구조 (실 DOM 확인됨)
        modal_selectors = [
            ".w2floatingLayer.confpop", ".confpop", ".w2floatingLayer",
            ".w2window", ".w2modal", "div[class*='w2alert']", "div[class*='w2confirm']",
            ".popup_confirm", ".popup_confirm_box", ".w2wframe_popup",
            "div[class*='popup']", "div[class*='modal']", "div[class*='dialog']",
        ]
        consent_kws = ["이미 서면 동의", "서면 동의를 받은", "출력하실 수 없습니다", "동의서를 출력하실 수", "2개월 이후"]
        recent_kws = ["재입력", "당사에 입력된", "최근 2개월", "이미 등록된 고객"]

        for f in self._modal_search_targets(dialog):
            for sel in modal_selectors:
                try:
                    loc = f.locator(sel)
                    cnt = loc.count()
                except Exception:
                    continue
                for idx in range(min(cnt, 15)):
                    try:
                        el = loc.nth(idx)
                        if not el.is_visible():
                            continue
                        text = (el.inner_text() or "").strip()
                    except Exception:
                        continue
                    if not text:
                        continue

                    # WebSquare 모달 텍스트는 단어 사이에 비분리공백(\xa0) 등을 쓰므로
                    # 모든 공백을 제거하고 비교한다.
                    text_compact = re.sub(r"\s", "", text)
                    is_consent = any(re.sub(r"\s", "", k) in text_compact for k in consent_kws)
                    is_recent = any(re.sub(r"\s", "", k) in text_compact for k in recent_kws)
                    if not (is_consent or is_recent):
                        continue

                    # 메인 동의서 출력 폼/그리드(입력칸 포함)를 잡은 경우는 알림 모달이 아니므로 skip
                    try:
                        if el.locator("input[id*='iptCustNm'], input[id*='iptCustIdno']").count() > 0:
                            continue
                    except Exception:
                        pass

                    kind = "consent_block" if is_consent else "recent_input"
                    birth6 = self._extract_birthdate(text) if is_consent else ""
                    self.last_block_msg = text  # 출력 차단 'N번째 고객' 순번 파싱용
                    self.log.info(f"[중복모달] 감지 kind={kind} sel={sel} text={text[:120]}")
                    try:
                        html = el.evaluate("e => e.outerHTML") or ""
                        if html:
                            self.log.info(f"[중복모달] outerHTML(앞 800자): {html[:800]}")
                    except Exception:
                        pass

                    btn_texts = ["확인", "닫기"] if kind == "consent_block" else ["아니오", "취소", "닫기", "확인"]
                    clicked = self._click_modal_button(f, el, btn_texts)
                    if clicked:
                        self.log.info(f"[중복모달] '{btn_texts[0]}' 계열 버튼 클릭 완료 (kind={kind})")
                    else:
                        self.log.warning(f"[중복모달] 버튼 클릭 실패(kind={kind}) — 위 outerHTML로 셀렉터 보정 필요")
                    time.sleep(0.8)
                    return True, kind, birth6
        return False, "", ""

    def _clear_multi_row_by_birthdate(self, birth6: str) -> bool:
        """다중입력 그리드에서 주민번호가 birth6 로 시작하는 행의 주민번호/성명 입력을 비운다.
        성공 시 True. (서면동의 완료로 출력 차단된 고객을 배치에서 제외하기 위함)
        """
        if not birth6:
            return False
        frames = [self.page] + list(self.page.frames)
        for f in frames:
            try:
                els = f.query_selector_all("input[id*='iptCustIdno']")
            except Exception:
                continue
            for el in els:
                try:
                    raw = el.input_value() or ""
                except Exception:
                    continue
                digits = raw.replace("-", "").replace(" ", "")
                if digits and digits.startswith(birth6):
                    el_id = el.get_attribute("id") or ""
                    try:
                        el.fill("")
                        el.evaluate("""e => { e.dispatchEvent(new Event('change',{bubbles:true})); e.dispatchEvent(new Event('input',{bubbles:true})); }""")
                    except Exception:
                        try:
                            el.evaluate("e => { e.value=''; }")
                        except Exception:
                            pass
                    # 같은 행의 성명 입력칸도 비움 (id 의 iptCustIdno -> iptCustNm 치환)
                    if el_id and "iptCustIdno" in el_id:
                        name_id = el_id.replace("iptCustIdno", "iptCustNm")
                        try:
                            nel = f.query_selector(f"[id='{name_id}']")
                            if nel:
                                nel.fill("")
                                nel.evaluate("""e => { e.dispatchEvent(new Event('change',{bubbles:true})); e.dispatchEvent(new Event('input',{bubbles:true})); }""")
                        except Exception:
                            pass
                    self.log.info(f"[다중입력] 서면동의 완료 고객(생년월일 {birth6}) 행을 비웠습니다. (id={el_id})")
                    return True
        self.log.warning(f"[다중입력] 생년월일 {birth6} 고객의 입력 행을 찾지 못해 비우지 못했습니다.")
        return False

    def _check_duplicate_popup(self, dialog) -> bool:
        """입력/출력 시점에 뜨는 중복·차단 알림(서면동의 완료 / 최근입력 재입력)을 감지·처리한다.
        네이티브 alert 와 WebSquare DOM 모달을 모두 확인한다. 감지 시 last_skip_reason 설정 후 True.
        """
        # 1) 네이티브 alert (혹시 native 로 뜬 경우)
        msg = getattr(self, "last_dialog_alert", None)
        if msg:
            self.last_dialog_alert = None
            if self._is_consent_block_alert(msg):
                self.last_skip_reason = "이미 서면 동의를 받은 고객(2개월 이내)"
            else:
                self.last_skip_reason = "최근 등록 이력 존재(2개월 이내)"
            self.log.info(f"[중복알림-native] {msg[:120]}")
            return True

        # 2) WebSquare DOM 모달
        try:
            handled, kind, _birth = self._detect_block_modal(dialog)
        except Exception as e:
            self.log.debug(f"[중복모달] 입력시점 감지 예외(무시): {e}")
            return False
        if handled:
            self.last_skip_reason = (
                "이미 서면 동의를 받은 고객(2개월 이내)" if kind == "consent_block"
                else "최근 등록 이력 존재(2개월 이내)"
            )
            return True
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
            
            # 팝업 창에도 동일한 대화상자 감지 리스너 등록
            def handle_popup_dialog(dialog_obj):
                msg = dialog_obj.message
                self.log.info(f"[알림 감지 - 팝업] 경고창 처리 시작: {msg}")
                self.last_dialog_alert = msg
                if "재입력" in msg or "당사에 입력된" in msg or "최근" in msg:
                    self.log.info("[알림 감지 - 팝업] 최근 입력 이력 중복창이므로 dismiss(아니오/취소) 처리합니다.")
                    dialog_obj.dismiss()
                else:
                    self.log.info("[알림 감지 - 팝업] 일반 경고/이미 서면 동의 알림이므로 accept(확인) 처리합니다.")
                    dialog_obj.accept()
            popup.on("dialog", handle_popup_dialog)
            
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
        # 만약 dialog가 None인 경우, 실제 동의서 출력 화면이 열려 있는 프레임을 찾습니다.
        target_frames = []
        if dialog is None:
            for f in [self.page] + list(self.page.frames):
                try:
                    if f.locator("[id*='iptCustNm'], [id*='iptCustIdno']").count() > 0:
                        target_frames.append(f)
                except Exception:
                    continue
            if not target_frames:
                self.log.info("열려 있는 동의서 출력 모달이 없습니다. 건너뜁니다.")
                return
        else:
            # dialog가 Frame 또는 Page인 경우 해당 객체만 검색 대상으로 설정
            target_frames = [dialog]
            # 만약 dialog가 메인 page이면, 모든 프레임을 검색 대상에 넣되, 동의서가 있는 프레임 위주로 검색하도록 함
            if dialog == self.page:
                target_frames = [self.page] + list(self.page.frames)

        self.log.info("모달 창을 닫기 위해 '닫기' 및 '창닫기(X)' 버튼을 탐색합니다...")
        try:
            for f in target_frames:
                # 단, 해당 프레임에 동의서 관련 요소(iptCustNm 등)가 있는지 먼저 체크하여,
                # 동의서가 없는 프레임의 닫기 버튼(예: 메인 화면의 닫기 버튼)을 잘못 클릭하는 일을 원천 차단합니다.
                # dialog가 page일 경우, 여러 프레임 중 동의서 입력폼이 있는 프레임만 필터링합니다.
                if f != self.page:
                    try:
                        if f.locator("[id*='iptCustNm'], [id*='iptCustIdno']").count() == 0:
                            continue
                    except Exception:
                        continue
                
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
            self.last_dialog_alert = None  # 출력 직전 알림 상태 리셋
            try:
                self._click_print_btn(dialog, print_btn)
            except Exception as e:
                raise RetryableError(f"'출력' 버튼 클릭 실패: {e}")
            # 출력 후 '차단 모달' 과 'OZ 뷰어' 등장을 동시에 감시(차단 알림이 늦게 떠도 안전하게 SKIP)
            oz_open_timeout = float(self.ins.get("oz", {}).get("open_timeout_sec", 20))
            state, _ = self._await_output_or_block(dialog, timeout=oz_open_timeout)
            if state == "blocked":
                self.last_skip_reason = "이미 서면 동의를 받은 고객(2개월 이내)"
                self.log.info(f"[{name}] 출력 시점 서면동의 완료 차단 알림 감지 → 등록을 건너뜁니다.")
                raise DuplicateCustomerError("이미 서면 동의를 받은 고객(2개월 이내)")
            if state == "timeout":
                raise RetryableError("출력 후 OZ 뷰어와 차단 알림이 모두 감지되지 않았습니다(타임아웃).")
            try:
                path = oz_viewer.save_as_pdf(str(dest), self.ins.get("oz", {}), self.log, file_format=actual_format)
            except Exception as e:
                # 저장 후처리(창닫기/덮어쓰기 대화상자 등)에서 예외가 나도 실제 결과 파일이
                # 생성됐으면 성공 처리하여 불필요한 재시도(=같은 고객 재출력)를 차단한다.
                try:
                    verified = self._verify(dest)
                    self.log.info(f"OZ 저장 중 예외가 있었으나 결과 파일이 확인되어 성공 처리합니다: {e}")
                    return verified
                except Exception:
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
            # OZ 실제 저장 명명({stem}.png, {stem}_N_1.png)을 표준형 {stem}_N.png 로 정규화.
            from . import oz_viewer
            if oz_viewer.normalize_png_pages(dest, self.log):
                return str(dest)
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
                    cnt = loc.count()
                    # .first 만 보면 stale(숨은) 팝업 버튼을 집을 수 있으므로, 보이는 매치를 찾는다.
                    for i in range(cnt):
                        el = loc.nth(i)
                        try:
                            if not el.is_visible():
                                continue
                        except Exception:
                            continue
                        el.click(force=True, timeout=3000)
                        self.log.info(f"[{action_name}] 셀렉터 '{selector}' 의 보이는 요소(idx={i})를 클릭했습니다. (frame={f.name})")
                        return
                except Exception as click_err:
                    self.log.warning(f"[{action_name}] 프레임 '{f.name}'에서 셀렉터 '{selector}' 클릭 실패 (스킵/계속): {click_err}")
                    continue
            self.log.info(f"[{action_name}] 모든 프레임에서 보이는 '{selector}'를 찾을 수 없어 자가 치유(Self-healing) 검색을 가동합니다.")

        keywords = ["출력", "인쇄", "인쇄하기", "출력하기", "저장", "프린트"]
        frames_to_search = [self.page] + list(self.page.frames)

        # WebSquare 출력 버튼은 <input type=button value='출력'> 형태라 text= 로는 안 잡힌다.
        # value 기반 + 텍스트 기반을 모두, '보이는' 요소로 한정해 클릭한다.
        for f in frames_to_search:
            for kw in keywords:
                for cand in (f"input[value='{kw}']", f"input[type='button'][value='{kw}']",
                             f"button:has-text('{kw}')", f"a:has-text('{kw}')", f"text={kw}"):
                    try:
                        loc = f.locator(cand)
                        cnt = loc.count()
                    except Exception:
                        continue
                    for i in range(cnt):
                        try:
                            el = loc.nth(i)
                            if not el.is_visible():
                                continue
                            el.click(force=True, timeout=2000)
                            self.log.info(f"[{action_name}] '{cand}' 의 보이는 요소 클릭 성공! (frame={f.name})")
                            return
                        except Exception:
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

    def upload_to_kb_scan(self, stamped_pdf_path) -> bool:
        """4단계: 서명/스탬프 작업을 마친 동의서를 KB EDMS 스캔 시스템에 자동 전송합니다."""
        import solting_auto
        solting_auto.check_stop()

        self.log.info("4단계: KB스캔 자동 업로드/전송을 시작합니다.")
        import os
        import shutil
        
        paths_list = stamped_pdf_path if isinstance(stamped_pdf_path, list) else [stamped_pdf_path]
        if not paths_list:
            self.log.warning("업로드할 파일 목록이 비어 있습니다.")
            return True

        first_path = Path(paths_list[0])
        filename = first_path.name
        stem = first_path.stem
        is_png = first_path.suffix.lower() == ".png"
        
        # 1) 파일 복사 (기본 문서 폴더 및 EDMS2 하위 폴더 둘 다 복사하여 접근 가능성 극대화)
        try:
            user_docs = Path(os.environ["USERPROFILE"]) / "Documents"
            edms_dir = user_docs / "EDMS2"
            edms_dir.mkdir(parents=True, exist_ok=True)
            
            for path_str in paths_list:
                p_path = Path(path_str)
                p_filename = p_path.name
                p_stem = p_path.stem
                p_is_png = p_path.suffix.lower() == ".png"
                
                if p_is_png:
                    # PNG일 경우 3개의 페이지 분할 이미지 복사
                    for suffix in ["_1.png", "_2.png", "_3.png"]:
                        src_file = p_path.parent / f"{p_stem}{suffix}"
                        if src_file.exists():
                            shutil.copy2(src_file, user_docs / f"{p_stem}{suffix}")
                            shutil.copy2(src_file, edms_dir / f"{p_stem}{suffix}")
                    self.log.info(f"EDMS 전송용 PNG 이미지 3장 복사 완료 (문서 및 EDMS2): {p_stem}")
                else:
                    # PDF일 경우 단일 파일 복사
                    if p_path.exists():
                        shutil.copy2(p_path, user_docs / p_filename)
                        shutil.copy2(p_path, edms_dir / p_filename)
                        self.log.info(f"EDMS 전송용 PDF 파일 복사 완료 (문서 및 EDMS2): {p_filename}")
            
            target_pdf_path = edms_dir / filename
        except Exception as copy_err:
            self.log.error(f"EDMS 파일 복사 중 예외 발생: {copy_err}")
            target_pdf_path = first_path
            
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
                filename = pdf_path.name
                stem = pdf_path.stem
                is_png = pdf_path.suffix.lower() == ".png"
                
                if is_png:
                    first_page = pdf_path.parent / f"{stem}_1.png"
                    if first_page.exists():
                        for suffix in ["_1.png", "_2.png", "_3.png"]:
                            src_file = pdf_path.parent / f"{stem}{suffix}"
                            if src_file.exists():
                                shutil.copy2(src_file, user_docs / f"{stem}{suffix}")
                                shutil.copy2(src_file, edms_dir / f"{stem}{suffix}")
                        self.log.info(f"EDMS 전송용 PNG 이미지 3장 복사 완료 (문서 및 EDMS2): {stem}")
                    else:
                        self.log.warning(f"복사할 원본 PNG 파일(1페이지)이 존재하지 않습니다: {first_page}")
                else:
                    if pdf_path.exists():
                        shutil.copy2(pdf_path, user_docs / filename)
                        shutil.copy2(pdf_path, edms_dir / filename)
                        self.log.info(f"EDMS 전송용 PDF 파일 복사 완료 (문서 및 EDMS2): {filename}")
                    else:
                        self.log.warning(f"복사할 원본 PDF 파일이 존재하지 않습니다: {pdf_path_str}")
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
