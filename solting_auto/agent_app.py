import os
import sys
import time
import uuid
import hashlib
import logging
import threading
import subprocess
import platform
from pathlib import Path
from dotenv import load_dotenv

from PyQt6 import QtWidgets, QtCore, QtGui
from supabase import create_client, Client

# Root directory configuration
FROZEN = getattr(sys, "frozen", False)
RES_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).resolve().parent.parent

ROOT = RES_DIR
sys.path.insert(0, str(ROOT))
load_dotenv(dotenv_path=APP_DIR / ".env")

# config.yaml 자동 복사 (존재하지 않는 경우 예시용을 복사)
config_path = APP_DIR / "config.yaml"
if not config_path.exists():
    import shutil
    example_path = RES_DIR / "config.example.yaml"
    if example_path.exists():
        try:
            shutil.copy2(example_path, config_path)
            print("[설정] config.example.yaml을 config.yaml로 자동 복사했습니다.")
        except Exception:
            pass

from solting_auto.runner import process_file, STAGE_SOLTING, STAGE_INSURANCE
from solting_auto.reporter import SUCCESS, FAIL, SKIP
from solting_auto.logger import get_logger

SUPABASE_URL = os.environ.get("NEXT_PUBLIC_SUPABASE_URL") or "https://eryswnijlvkzpeamjtqu.supabase.co"
SUPABASE_KEY = os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY") or "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImVyeXN3bmlqbHZrenBlYW1qdHF1Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODA1NDczNzksImV4cCI6MjA5NjEyMzM3OX0._H3dnDLym1fBkYMXHXUQp-4JJzl0GxElT8sl5odJNsQ"

OFFSET_LABELS = {
    "image_add_x": "이미지 추가 버튼 X 좌표",
    "image_add_y": "이미지 추가 버튼 Y 좌표",
    "select_all_x": "전체선택 버튼 X 좌표",
    "select_all_y": "전체선택 버튼 Y 좌표",
    "send_x": "전송 버튼 X 좌표",
    "send_y": "전송 버튼 Y 좌표",
    "tab_local_pdf_x": "로컬 PDF 탭 X 좌표",
    "tab_local_pdf_y": "로컬 PDF 탭 Y 좌표",
    "folder_docs_x": "문서 폴더 노드 X 좌표",
    "folder_docs_y": "문서 폴더 노드 Y 좌표",
    "search_input_x": "파일검색 입력창 X 좌표",
    "search_input_y": "파일검색 입력창 Y 좌표",
    "search_btn_x": "파일검색 돋보기 X 좌표",
    "search_btn_y": "파일검색 돋보기 Y 좌표",
    "confirm_btn_x": "다이얼로그 확인 버튼 X 좌표",
    "confirm_btn_y": "다이얼로그 확인 버튼 Y 좌표",
    "pop_send_btn_x": "전송확인 팝업 전송버튼 X 좌표",
    "pop_send_btn_y": "전송확인 팝업 전송버튼 Y 좌표",
    "fallback_pop_send_x": "팝업 미감지 대비 전송버튼 X 좌표",
    "fallback_pop_send_y": "팝업 미감지 대비 전송버튼 Y 좌표"
}

DELAY_LABELS = {
    "dialog_open_wait": "이미지 추가 창 대기 시간 (초)",
    "tab_click_wait": "로컬 PDF 탭 클릭 후 대기 시간 (초)",
    "folder_expand_wait": "문서 폴더 더블클릭 후 대기 시간 (초)",
    "search_wait": "돋보기 검색 클릭 후 대기 시간 (초)",
    "image_load_wait": "확인 버튼 클릭 후 이미지 로딩 대기 (초)",
    "select_all_wait": "전체선택 클릭 후 대기 시간 (초)",
    "send_confirm_wait": "전송 클릭 후 확인 팝업 대기 시간 (초)",
    "success_alert_wait": "성공 완료 알림창 대기 시간 (초)"
}

def get_hwid():
    """WMI or Registry based HWID generation for Windows."""
    try:
        cmd = 'reg query "HKLM\\SOFTWARE\\Microsoft\\Cryptography" /v MachineGuid'
        out = subprocess.check_output(cmd, shell=True, text=True)
        for line in out.splitlines():
            if "MachineGuid" in line:
                guid = line.split()[-1]
                return guid.strip()
    except Exception:
        pass
    mac = uuid.getnode()
    return hashlib.sha256(str(mac).encode()).hexdigest()


class Signaler(QtCore.QObject):
    log_signal = QtCore.pyqtSignal(str)
    progress_signal = QtCore.pyqtSignal(int, int, str)
    finished_signal = QtCore.pyqtSignal(bool, str, str) # success, msg, report_url


def _attach_file_log(logger, config):
    """자동화 로거에 디스크 파일 핸들러(output/run.log, 민감정보 마스킹)를 부착하고 반환.
    GUI 실행 시에도 로그가 파일로 남아 사후 진단이 가능하도록 한다."""
    try:
        from solting_auto.logger import MaskingFilter
        out_dir = Path((config or {}).get("run", {}).get("output_folder", str(APP_DIR / "output")))
        out_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(out_dir / "run.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
        fh.addFilter(MaskingFilter())
        logger.addHandler(fh)
        return fh
    except Exception:
        return None


def _detach_file_log(logger, fh):
    if fh is not None:
        try:
            logger.removeHandler(fh)
            fh.close()
        except Exception:
            pass


class PyQtLogHandler(logging.Handler):
    def __init__(self, signaler):
        super().__init__()
        self.signaler = signaler

    def emit(self, record):
        msg = self.format(record)
        self.signaler.log_signal.emit(msg)


class MouseTracker(QtCore.QThread):
    position_signal = QtCore.pyqtSignal(int, int)

    def __init__(self):
        super().__init__()
        self.running = False

    def run(self):
        import pyautogui
        self.running = True
        while self.running:
            try:
                x, y = pyautogui.position()
                self.position_signal.emit(x, y)
            except Exception:
                pass
            self.msleep(100)

    def stop(self):
        self.running = False
        self.wait()


class AutomationWorker(QtCore.QThread):
    def __init__(self, xlsx_path, config, dry_run, signaler, supabase_client, log_id):
        super().__init__()
        self.xlsx_path = xlsx_path
        self.config = config
        self.dry_run = dry_run
        self.signaler = signaler
        self.supabase = supabase_client
        self.log_id = log_id
        self.active_auto = None
        self.polling_active = True

    def run(self):
        import solting_auto
        solting_auto.register_stop_check(lambda: getattr(self, "stop_requested", False))

        # Start remote stop polling thread
        polling_thread = threading.Thread(target=self._poll_stop_status, daemon=True)
        polling_thread.start()

        # Build log-capturing custom logger
        logger = logging.getLogger("kkandori_agent")
        logger.setLevel(logging.INFO)
        handler = PyQtLogHandler(self.signaler)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(handler)

        # 디스크 파일 로깅(진단용): output/run.log (민감정보 마스킹)
        file_handler = _attach_file_log(logger, self.config)

        def progress_cb(done, total, last_result):
            self.signaler.progress_signal.emit(done, total, f"[{last_result.row_no}행] {last_result.status}")
            # Update DB progress in real-time via RPC
            try:
                self.supabase.rpc("update_execution_log_progress_via_device", {
                    "p_log_id": self.log_id,
                    "p_done": done,
                    "p_total": total,
                    "p_last_message": f"[{last_result.row_no}행] {last_result.status}"
                }).execute()
            except Exception:
                pass

        try:
            # Update status in DB to running via RPC (일시적 소켓오류는 무시 — 자동화는 계속)
            try:
                self.supabase.rpc("update_execution_log_status_via_device", {
                    "p_log_id": self.log_id,
                    "p_status": "running",
                    "p_error_reason": None,
                    "p_error_screenshot_url": None,
                    "p_report_file_url": None
                }).execute()
            except Exception as _st_err:
                logger.warning(f"실행상태(running) 업데이트 일시 실패(무시하고 진행): {_st_err}")

            # Run Solting core automation suite
            summary = process_file(
                self.xlsx_path, 
                self.config, 
                logger, 
                dry_run=self.dry_run, 
                progress_cb=progress_cb,
                stop_check_cb=lambda: getattr(self, "stop_requested", False)
            )

            # Check if user requested stop
            if hasattr(self, "stop_requested") and self.stop_requested:
                self.supabase.rpc("update_execution_log_status_via_device", {
                    "p_log_id": self.log_id,
                    "p_status": "stopped",
                    "p_error_reason": "사용자 로컬 중단 요청",
                    "p_error_screenshot_url": None,
                    "p_report_file_url": None
                }).execute()
                self.signaler.finished_signal.emit(False, "사용자 중단 요청으로 종료되었습니다.", "")
                return

            # Find latest output report
            out_dir = Path(self.config["run"].get("output_folder", "./output"))
            reports = sorted(out_dir.glob("result_*.xlsx"), key=lambda p: p.stat().st_mtime)
            report_path = str(reports[-1].resolve()) if reports else ""

            # Upload report to Supabase Storage if found
            report_url = ""
            if report_path:
                try:
                    file_name = Path(report_path).name
                    with open(report_path, "rb") as f:
                        self.supabase.storage.from_("error-screenshots").upload(
                            path=f"reports/{self.log_id}_{file_name}",
                            file=f,
                            file_options={"content-type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
                        )
                    report_url = self.supabase.storage.from_("error-screenshots").get_public_url(f"reports/{self.log_id}_{file_name}")
                except Exception as upload_err:
                    logger.error(f"결과 엑셀 리포트 업로드 실패: {upload_err}")

            # Update status in DB to success via RPC
            self.supabase.rpc("update_execution_log_status_via_device", {
                "p_log_id": self.log_id,
                "p_status": "success",
                "p_error_reason": None,
                "p_error_screenshot_url": None,
                "p_report_file_url": report_url
            }).execute()

            self.signaler.finished_signal.emit(True, f"성공적으로 완료되었습니다! {summary.as_text()}", report_url)

        except Exception as e:
            # Check if this was a user-initiated stop
            is_stopped = False
            if "사용자 중단 요청" in str(e) or (hasattr(self, "stop_requested") and self.stop_requested):
                is_stopped = True

            screenshot_url = ""
            # Take screenshot and upload on failure
            if not is_stopped:
                try:
                    import pyautogui
                    out_dir = Path(self.config.get("run", {}).get("output_folder", str(APP_DIR / "output")))
                    shot_path = out_dir / f"error_{self.log_id}.png"
                    shot_path.parent.mkdir(parents=True, exist_ok=True)
                    pyautogui.screenshot(str(shot_path))
                    
                    with open(shot_path, "rb") as f:
                        self.supabase.storage.from_("error-screenshots").upload(
                            path=f"errors/{self.log_id}.png",
                            file=f,
                            file_options={"content-type": "image/png"}
                        )
                    screenshot_url = self.supabase.storage.from_("error-screenshots").get_public_url(f"errors/{self.log_id}.png")
                except Exception as shot_err:
                    logger.error(f"에러 화면 캡처 업로드 실패: {shot_err}")

            # Update DB with error status via RPC
            try:
                self.supabase.rpc("update_execution_log_status_via_device", {
                    "p_log_id": self.log_id,
                    "p_status": "stopped" if is_stopped else "failed",
                    "p_error_reason": str(e),
                    "p_error_screenshot_url": screenshot_url,
                    "p_report_file_url": None
                }).execute()
            except Exception:
                pass

            if is_stopped:
                self.signaler.finished_signal.emit(False, "사용자 요청에 의해 작업이 정지되었습니다.", "")
            else:
                self.signaler.finished_signal.emit(False, f"작업 중 오류 발생: {e}", "")
        finally:
            self.polling_active = False
            logger.removeHandler(handler)
            _detach_file_log(logger, file_handler)

    def _poll_stop_status(self):
        """ light background polling to check if admin requested stop from Supabase """
        while self.polling_active:
            try:
                res = self.supabase.rpc("check_execution_log_status", {"p_log_id": self.log_id}).execute()
                if res.data == "stopped":
                    print("[POLLING] 원격 중단 신호 감지!")
                    self.stop_requested = True
                    import solting_auto.insurance
                    self.stop_requested = True
            except Exception:
                pass
            time.sleep(2)


class EDMSUploadWorker(QtCore.QThread):
    def __init__(self, pdf_paths, config, signaler, supabase_client, log_id):
        super().__init__()
        self.pdf_paths = pdf_paths
        self.config = config
        self.signaler = signaler
        self.supabase = supabase_client
        self.log_id = log_id
        self.stop_requested = False
        self.polling_active = True

    def run(self):
        import solting_auto
        solting_auto.register_stop_check(lambda: getattr(self, "stop_requested", False))

        # Start remote stop polling thread
        polling_thread = threading.Thread(target=self._poll_stop_status, daemon=True)
        polling_thread.start()

        # Build log-capturing custom logger
        logger = logging.getLogger("kkandori_edms")
        logger.setLevel(logging.INFO)
        handler = PyQtLogHandler(self.signaler)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(handler)

        # 디스크 파일 로깅(진단용): output/run.log (민감정보 마스킹)
        file_handler = _attach_file_log(logger, self.config)

        def progress_cb(done, total, last_msg):
            self.signaler.progress_signal.emit(done, total, last_msg)
            # Update DB progress in real-time via RPC
            try:
                self.supabase.rpc("update_execution_log_progress_via_device", {
                    "p_log_id": self.log_id,
                    "p_done": done,
                    "p_total": total,
                    "p_last_message": last_msg
                }).execute()
            except Exception:
                pass

        try:
            # Update status in DB to running via RPC
            self.supabase.rpc("update_execution_log_status_via_device", {
                "p_log_id": self.log_id,
                "p_status": "running",
                "p_error_reason": None,
                "p_error_screenshot_url": None,
                "p_report_file_url": None
            }).execute()

            from solting_auto.insurance import InsuranceAutomation
            # Instantiate helper module (bypasses full playwright sync because we do not enter __enter__)
            auto = InsuranceAutomation(self.config, logger)
            auto.stop_requested = False

            # Inject stop link
            self.active_auto = auto

            # Run win32 macro for batch upload
            success = auto.batch_upload_via_win32(self.pdf_paths, progress_cb=progress_cb)

            # Check if user requested stop
            if self.stop_requested or auto.stop_requested:
                self.supabase.rpc("update_execution_log_status_via_device", {
                    "p_log_id": self.log_id,
                    "p_status": "stopped",
                    "p_error_reason": "사용자 로컬 중단 요청",
                    "p_error_screenshot_url": None,
                    "p_report_file_url": None
                }).execute()
                self.signaler.finished_signal.emit(False, "사용자 중단 요청으로 종료되었습니다.", "")
                return

            if success:
                # Update status in DB to success via RPC
                self.supabase.rpc("update_execution_log_status_via_device", {
                    "p_log_id": self.log_id,
                    "p_status": "success",
                    "p_error_reason": None,
                    "p_error_screenshot_url": None,
                    "p_report_file_url": None
                }).execute()
                self.signaler.finished_signal.emit(True, f"성공적으로 {len(self.pdf_paths)}개 파일을 전송했어요!", "")
            else:
                raise RuntimeError("전송에 성공한 파일이 없습니다. 설정을 확인해 주세요.")

        except Exception as e:
            is_stopped = False
            if self.stop_requested or "중단" in str(e):
                is_stopped = True

            # Update DB with error status via RPC
            try:
                self.supabase.rpc("update_execution_log_status_via_device", {
                    "p_log_id": self.log_id,
                    "p_status": "stopped" if is_stopped else "failed",
                    "p_error_reason": str(e),
                    "p_error_screenshot_url": None,
                    "p_report_file_url": None
                }).execute()
            except Exception:
                pass

            if is_stopped:
                self.signaler.finished_signal.emit(False, "사용자 요청에 의해 작업이 정지되었습니다.", "")
            else:
                self.signaler.finished_signal.emit(False, f"작업 중 오류 발생: {e}", "")
        finally:
            self.polling_active = False
            logger.removeHandler(handler)
            _detach_file_log(logger, file_handler)

    def _poll_stop_status(self):
        """ light background polling to check if admin requested stop from Supabase """
        while self.polling_active:
            try:
                res = self.supabase.rpc("check_execution_log_status", {"p_log_id": self.log_id}).execute()
                if res.data == "stopped":
                    print("[POLLING] EDMS 원격 중단 신호 감지!")
                    self.stop_requested = True
                    if hasattr(self, "active_auto") and self.active_auto:
                        self.active_auto.stop_requested = True
            except Exception:
                pass
            time.sleep(2)


class LoginWorker(QtCore.QThread):
    finished_signal = QtCore.pyqtSignal(bool, str)

    def __init__(self, username, password, birthdate):
        super().__init__()
        self.username = username
        self.password = password
        self.birthdate = birthdate

    def run(self):
        try:
            from solting_auto.config import load_config
            from solting_auto.insurance import InsuranceAutomation
            
            cfg = load_config(str(APP_DIR / "config.yaml"))
            cfg["insurance"]["browser"]["mode"] = "attach"
            cfg["insurance"]["browser"]["cdp_url"] = "http://localhost:9222"
            cfg["insurance"]["browser"]["skip_login"] = False
            
            logger = logging.getLogger("kkandori_agent")
            
            with InsuranceAutomation(cfg, logger) as auto:
                auto.login(username=self.username, password=self.password, birthdate=self.birthdate, force=True)
            self.finished_signal.emit(True, "자동 로그인에 성공했어요! 🔑")
        except Exception as e:
            self.finished_signal.emit(False, f"자동 로그인 실패: {e}")


class CalibrateWorker(QtCore.QThread):
    finished_signal = QtCore.pyqtSignal(bool, dict, str)

    def run(self):
        try:
            import requests
            res = requests.post("http://127.0.0.1:8000/edms/calibrate", timeout=12)
            if res.status_code == 200:
                data = res.json()
                if data.get("success"):
                    self.finished_signal.emit(True, data.get("detected", {}), "")
                else:
                    self.finished_signal.emit(False, {}, data.get("error", "알 수 없는 오류"))
            else:
                self.finished_signal.emit(False, {}, f"HTTP 에러 코드: {res.status_code}")
        except Exception as e:
            self.finished_signal.emit(False, {}, f"로컬 웹 서버가 기동되어 있지 않습니다. 먼저 웹 서버를 실행해 주세요.\n({e})")


class CustomerCrawlWorker(QtCore.QThread):
    """KB 보장분석 고객 데이터 수집 + Supabase 고객DB 업로드 워커."""
    log_signal = QtCore.pyqtSignal(str)
    progress_signal = QtCore.pyqtSignal(int, int, str)
    rows_signal = QtCore.pyqtSignal(list)            # 미리보기용 정규화 행
    finished_signal = QtCore.pyqtSignal(bool, str, int)  # success, msg, saved_count

    def __init__(self, cdp_url, tenant_id, device_id, supabase_client, dump_path,
                 contact_excel_paths=None):
        super().__init__()
        self.cdp_url = cdp_url
        self.tenant_id = tenant_id
        self.device_id = device_id
        self.supabase = supabase_client
        self.dump_path = dump_path
        self.contact_excel_paths = contact_excel_paths or []
        self.stop_requested = False

    def run(self):
        logger = logging.getLogger("kkandori_crawl")
        logger.setLevel(logging.INFO)
        handler = PyQtLogHandler(self)  # self.log_signal 로 라우팅
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
        saved = 0
        try:
            from solting_auto import kb_crawler
            records = kb_crawler.crawl_customers(
                cdp_url=self.cdp_url,
                logger=logger,
                progress_cb=lambda d, t, m: self.progress_signal.emit(d, t, m),
                stop_cb=lambda: self.stop_requested,
                dump_path=self.dump_path,
                contact_excel_paths=self.contact_excel_paths,
            )

            self.rows_signal.emit(records or [])

            if self.stop_requested:
                self.finished_signal.emit(False, "사용자 중단 요청으로 수집을 멈췄습니다.", 0)
                return
            if not records:
                self.finished_signal.emit(
                    False,
                    "수집된 고객 데이터가 없습니다.\nKB 보장분석 화면에서 '조회'를 실행한 상태로 다시 시도해 주세요.\n(원본 응답 덤프가 output 폴더에 저장되었습니다.)",
                    0,
                )
                return

            # 서버 업로드 (100건씩 일괄 upsert)
            CHUNK = 100
            total = len(records)
            for i in range(0, total, CHUNK):
                if self.stop_requested:
                    break
                chunk = records[i:i + CHUNK]
                try:
                    res = self.supabase.rpc("upsert_customer_records_via_device", {
                        "p_tenant_id": self.tenant_id,
                        "p_device_id": self.device_id,
                        "p_records": chunk,
                    }).execute()
                    n = res.data if isinstance(res.data, int) else len(chunk)
                    saved += (n or 0)
                    self.progress_signal.emit(min(i + CHUNK, total), total, f"서버 고객DB 저장 {saved}건")
                except Exception as up_err:
                    logger.error(f"[수집] 서버 저장 실패: {up_err}")
                    self.finished_signal.emit(False, f"서버 업로드 실패: {up_err}", saved)
                    return

            self.finished_signal.emit(True, f"고객 {saved}건을 어드민 고객DB에 저장했습니다. 🎉", saved)
        except Exception as e:
            logger.error(f"[수집] 오류: {e}")
            self.finished_signal.emit(False, f"수집 중 오류가 발생했습니다:\n{e}", saved)
        finally:
            try:
                logger.removeHandler(handler)
            except Exception:
                pass


class KkandoriAgent(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("깐돌이 전산등록 자동화 에이전트")
        self.resize(580, 780)
        self.supabase = None
        self.tenant = None
        self.device = None
        self.tenants_list = []
        self.hwid = get_hwid()
        self.current_job_worker = None
        self.current_edms_worker = None
        self.edms_pdf_paths = []
        
        self.signaler = Signaler()
        self.signaler.log_signal.connect(self.log_message)
        self.signaler.progress_signal.connect(self.update_progress)
        self.signaler.finished_signal.connect(self.automation_finished)

        self.signaler_edms = Signaler()
        self.signaler_edms.log_signal.connect(self.log_edms_message)
        self.signaler_edms.progress_signal.connect(self.update_edms_progress)
        self.signaler_edms.finished_signal.connect(self.edms_finished)

        self.mouse_tracker = MouseTracker()
        self.mouse_tracker.position_signal.connect(self.update_mouse_pos)

        # Heartbeat timer
        self.heartbeat_timer = QtCore.QTimer(self)
        self.heartbeat_timer.timeout.connect(self.send_heartbeat)

        self.init_ui()
        self.load_initial_values()
        self.connect_supabase()
        
        # Shortcut handler
        self.shortcut_stop = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+D"), self)
        self.shortcut_stop.activated.connect(self.handle_ctrl_d_shortcut)

        # Check status & auto-login
        self.check_device_status_on_startup()

    def init_ui(self):
        # Central Tab Widget
        self.tabs = QtWidgets.QTabWidget()
        self.setCentralWidget(self.tabs)

        # Tab 1: Auth & Device Status
        self.tab_auth = QtWidgets.QWidget()
        self.init_auth_tab()
        self.tabs.addTab(self.tab_auth, "🔐 기기 인증")

        # Tab 2: Automation Main Control (Unified Consent & EDMS)
        self.tab_automation = QtWidgets.QWidget()
        self.init_automation_tab()
        self.tabs.addTab(self.tab_automation, "🚀 자동화 실행")
        self.tabs.setTabEnabled(1, False)

        # Tab 3: Configuration (Offsets & Delays Unified)
        self.tab_settings = QtWidgets.QWidget()
        self.init_settings_tab()
        self.tabs.addTab(self.tab_settings, "⚙️ 설정")
        self.tabs.setTabEnabled(2, False)

        # Tab 4: Customer DB Crawl (KB 보장분석 수집)
        self.tab_customerdb = QtWidgets.QWidget()
        self.init_customerdb_tab()
        self.tabs.addTab(self.tab_customerdb, "🗂️ 고객DB 수집")
        self.tabs.setTabEnabled(3, False)

        # Tab 5: User Guides (Always enabled so users can read before authentication)
        self.tab_guide = QtWidgets.QWidget()
        self.init_guide_tab()
        self.tabs.addTab(self.tab_guide, "📖 사용 가이드")

        # Status Bar
        self.statusBar().showMessage(f"기기 식별자(HWID): {self.hwid}")

    def init_auth_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_auth)
        layout.setContentsMargins(40, 30, 40, 30)
        layout.setSpacing(15)

        # Mascot hamster helper logo
        self.lbl_mascot = QtWidgets.QLabel()
        mascot_path = os.path.join(os.path.dirname(__file__), "mascot.png")
        if os.path.exists(mascot_path):
            pixmap = QtGui.QPixmap(mascot_path)
            self.lbl_mascot.setPixmap(pixmap.scaled(140, 140, QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation))
        self.lbl_mascot.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_mascot)

        self.title_auth = QtWidgets.QLabel("깐돌이 전산 자동화 에이전트")
        self.title_auth.setFont(QtGui.QFont("Malgun Gothic", 16, QtGui.QFont.Weight.Bold))
        self.title_auth.setStyleSheet("color: white;")
        self.title_auth.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.title_auth)

        # Stacked layout for different statuses
        self.auth_stack = QtWidgets.QStackedWidget()
        layout.addWidget(self.auth_stack)

        # Page 0: Registration Page (Unregistered)
        self.page_register = QtWidgets.QWidget()
        reg_lay = QtWidgets.QVBoxLayout(self.page_register)
        reg_lay.setSpacing(10)
        
        lbl_reg_desc = QtWidgets.QLabel("아직 등록되지 않은 기기예요.\n아래에서 회사를 선택하고 기기 승인을 신청해 주세요.")
        lbl_reg_desc.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        lbl_reg_desc.setStyleSheet("color: #94a3b8; font-size: 10.5pt;")
        reg_lay.addWidget(lbl_reg_desc)

        form_group = QtWidgets.QGroupBox("기기 등록 정보")
        form_layout = QtWidgets.QFormLayout(form_group)
        
        self.combo_tenant = QtWidgets.QComboBox()
        self.input_device_name = QtWidgets.QLineEdit()
        self.input_device_name.setText(platform.node())
        self.input_device_name.setPlaceholderText("기기 이름을 입력해 주세요")

        form_layout.addRow("소속 회사:", self.combo_tenant)
        form_layout.addRow("기기 명칭:", self.input_device_name)
        reg_lay.addWidget(form_group)

        self.btn_register_device = QtWidgets.QPushButton("등록 신청하기")
        self.btn_register_device.clicked.connect(self.submit_registration)
        reg_lay.addWidget(self.btn_register_device)
        self.auth_stack.addWidget(self.page_register)

        # Page 1: Pending Page (Pending Approval)
        self.page_pending = QtWidgets.QWidget()
        pen_lay = QtWidgets.QVBoxLayout(self.page_pending)
        pen_lay.setSpacing(12)
        
        lbl_pen_title = QtWidgets.QLabel("기기 승인을 기다리고 있어요 🐹")
        lbl_pen_title.setFont(QtGui.QFont("Malgun Gothic", 12, QtGui.QFont.Weight.Bold))
        lbl_pen_title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        pen_lay.addWidget(lbl_pen_title)

        self.lbl_pen_desc = QtWidgets.QLabel("관리자가 기기 등록을 승인하면 발급되는\n6자리 간편 인증번호(PIN)를 입력해 주세요.")
        self.lbl_pen_desc.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.lbl_pen_desc.setStyleSheet("color: #94a3b8;")
        pen_lay.addWidget(self.lbl_pen_desc)

        self.input_pin_pending = QtWidgets.QLineEdit()
        self.input_pin_pending.setPlaceholderText("6자리 인증번호(PIN) 입력")
        self.input_pin_pending.setMaxLength(6)
        self.input_pin_pending.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.input_pin_pending.setFont(QtGui.QFont("Consolas", 14, QtGui.QFont.Weight.Bold))
        pen_lay.addWidget(self.input_pin_pending)

        btn_pen_row = QtWidgets.QHBoxLayout()
        self.btn_refresh_status = QtWidgets.QPushButton("🔄 상태 새로고침")
        self.btn_refresh_status.setStyleSheet("background-color: #475569;")
        self.btn_refresh_status.clicked.connect(self.check_device_status_on_startup)
        
        self.btn_login_pending = QtWidgets.QPushButton("🔐 인증번호로 로그인")
        self.btn_login_pending.clicked.connect(self.login_via_pending_pin)
        
        btn_pen_row.addWidget(self.btn_refresh_status)
        btn_pen_row.addWidget(self.btn_login_pending)
        pen_lay.addLayout(btn_pen_row)
        self.auth_stack.addWidget(self.page_pending)

        # Page 2: PIN Login Page (Approved but not verified in current session)
        self.page_pin_login = QtWidgets.QWidget()
        pin_lay = QtWidgets.QVBoxLayout(self.page_pin_login)
        pin_lay.setSpacing(12)

        lbl_pin_title = QtWidgets.QLabel("6자리 간편 인증번호 로그인")
        lbl_pin_title.setFont(QtGui.QFont("Malgun Gothic", 12, QtGui.QFont.Weight.Bold))
        lbl_pin_title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        pin_lay.addWidget(lbl_pin_title)

        lbl_pin_desc = QtWidgets.QLabel("발급받은 6자리 인증번호(PIN)를 입력해 주세요.")
        lbl_pin_desc.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        lbl_pin_desc.setStyleSheet("color: #94a3b8;")
        pin_lay.addWidget(lbl_pin_desc)

        self.input_pin_login = QtWidgets.QLineEdit()
        self.input_pin_login.setPlaceholderText("6자리 인증번호(PIN) 입력")
        self.input_pin_login.setMaxLength(6)
        self.input_pin_login.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.input_pin_login.setFont(QtGui.QFont("Consolas", 14, QtGui.QFont.Weight.Bold))
        pin_lay.addWidget(self.input_pin_login)

        self.btn_login_submit = QtWidgets.QPushButton("인증 및 로그인")
        self.btn_login_submit.clicked.connect(self.login_via_submit_pin)
        pin_lay.addWidget(self.btn_login_submit)
        self.auth_stack.addWidget(self.page_pin_login)

        # Page 3: Logged-in / Connected Page (Approved and authenticated)
        self.page_status = QtWidgets.QWidget()
        stat_lay = QtWidgets.QVBoxLayout(self.page_status)
        stat_lay.setSpacing(10)

        self.lbl_connected = QtWidgets.QLabel("에이전트가 안전하게 연결되었어요! 🎉")
        self.lbl_connected.setFont(QtGui.QFont("Malgun Gothic", 12, QtGui.QFont.Weight.Bold))
        self.lbl_connected.setStyleSheet("color: #10b981;")
        self.lbl_connected.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        stat_lay.addWidget(self.lbl_connected)

        self.info_browser = QtWidgets.QTextBrowser()
        self.info_browser.setMaximumHeight(150)
        self.info_browser.setStyleSheet("background-color: #1e293b; border: 1px solid #334155; color: white;")
        stat_lay.addWidget(self.info_browser)

        self.btn_logout = QtWidgets.QPushButton("로그아웃")
        self.btn_logout.setStyleSheet("background-color: #ef4444;")
        self.btn_logout.clicked.connect(self.logout_device)
        stat_lay.addWidget(self.btn_logout)
        self.auth_stack.addWidget(self.page_status)

        # Bottom block (blocked info)
        self.lbl_auth_status = QtWidgets.QLabel("")
        self.lbl_auth_status.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.lbl_auth_status.setStyleSheet("color: #f87171; font-weight: bold;")
        layout.addWidget(self.lbl_auth_status)

    def init_automation_tab(self):
        # 2개 뷰를 하나로 통합하고 토스 스타일로 전환
        layout = QtWidgets.QVBoxLayout(self.tab_automation)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        # Toss Style Segmented Toggle Bar
        toggle_lay = QtWidgets.QHBoxLayout()
        toggle_lay.setSpacing(4)
        
        self.btn_toggle_consent = QtWidgets.QPushButton("📄 동의서 등록 (1~3단계)")
        self.btn_toggle_consent.setFixedHeight(38)
        self.btn_toggle_consent.setStyleSheet("background-color: #2563eb; color: white; border-radius: 8px;")
        self.btn_toggle_consent.clicked.connect(lambda: self.switch_automation_view(0))

        self.btn_toggle_edms = QtWidgets.QPushButton("📤 EDMS 일괄 전송")
        self.btn_toggle_edms.setFixedHeight(38)
        self.btn_toggle_edms.setStyleSheet("background-color: #1e293b; color: #94a3b8; border-radius: 8px;")
        self.btn_toggle_edms.clicked.connect(lambda: self.switch_automation_view(1))

        toggle_lay.addWidget(self.btn_toggle_consent)
        toggle_lay.addWidget(self.btn_toggle_edms)
        layout.addLayout(toggle_lay)

        # Stacked Layout
        self.stack_automation = QtWidgets.QStackedWidget()
        layout.addWidget(self.stack_automation)

        # Page 1: Consent
        self.panel_consent = QtWidgets.QWidget()
        self.init_control_tab(self.panel_consent)
        self.stack_automation.addWidget(self.panel_consent)

        # Page 2: EDMS
        self.panel_edms = QtWidgets.QWidget()
        self.init_edms_tab(self.panel_edms)
        self.stack_automation.addWidget(self.panel_edms)

    def switch_automation_view(self, index):
        self.stack_automation.setCurrentIndex(index)
        if index == 0:
            self.btn_toggle_consent.setStyleSheet("background-color: #2563eb; color: white; border-radius: 8px;")
            self.btn_toggle_edms.setStyleSheet("background-color: #1e293b; color: #94a3b8; border-radius: 8px;")
        else:
            self.btn_toggle_consent.setStyleSheet("background-color: #1e293b; color: #94a3b8; border-radius: 8px;")
            self.btn_toggle_edms.setStyleSheet("background-color: #2563eb; color: white; border-radius: 8px;")

    def init_control_tab(self, parent_widget):
        main_layout = QtWidgets.QVBoxLayout(parent_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")

        scroll_widget = QtWidgets.QWidget()
        scroll_widget.setStyleSheet("QWidget { background-color: transparent; }")
        layout = QtWidgets.QVBoxLayout(scroll_widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # 1단계
        self.group_step1 = QtWidgets.QGroupBox("1단계: KB전산 브라우저 열기")
        step1_layout = QtWidgets.QVBoxLayout(self.group_step1)
        self.btn_open_edge = QtWidgets.QPushButton("🌐 Edge 브라우저 기동")
        self.btn_open_edge.setStyleSheet("background-color: #2563eb; color: white; padding: 8px; font-weight: bold;")
        self.btn_open_edge.clicked.connect(self.open_edge_browser)
        step1_layout.addWidget(self.btn_open_edge)
        layout.addWidget(self.group_step1)

        # 2단계
        self.group_step2 = QtWidgets.QGroupBox("2단계: 포털 자동 로그인")
        step2_layout = QtWidgets.QVBoxLayout(self.group_step2)
        step2_form = QtWidgets.QFormLayout()
        
        self.input_login_id = QtWidgets.QLineEdit()
        self.input_login_id.setPlaceholderText("설계사 ID")
        self.input_login_pw = QtWidgets.QLineEdit()
        self.input_login_pw.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.input_login_pw.setPlaceholderText("비밀번호")
        self.input_login_birth = QtWidgets.QLineEdit()
        self.input_login_birth.setPlaceholderText("생년월일 6자리")
        self.input_login_birth.setMaxLength(6)

        step2_form.addRow("설계사 ID:", self.input_login_id)
        step2_form.addRow("비밀번호:", self.input_login_pw)
        step2_form.addRow("생년월일:", self.input_login_birth)
        
        step2_layout.addLayout(step2_form)

        # 로그인 정보 저장 체크박스 추가
        self.check_save_credentials = QtWidgets.QCheckBox("포털 자동 로그인 정보 기억하기")
        self.check_save_credentials.setStyleSheet("color: #94a3b8; font-size: 9pt;")
        step2_layout.addWidget(self.check_save_credentials)
        
        self.btn_auto_login = QtWidgets.QPushButton("🔑 자동 로그인 실행")
        self.btn_auto_login.setStyleSheet("background-color: #2563eb; color: white; padding: 8px; font-weight: bold;")
        self.btn_auto_login.clicked.connect(self.run_auto_login)
        step2_layout.addWidget(self.btn_auto_login)
        layout.addWidget(self.group_step2)

        # 3단계
        self.group_step3 = QtWidgets.QGroupBox("3단계: 엑셀 및 저장 설정")
        step3_layout = QtWidgets.QVBoxLayout(self.group_step3)

        # 초기 단계별 흐름 제어 활성화 상태 세팅 (1단계만 켜고 2,3단계 잠금)
        self.group_step2.setEnabled(False)
        self.group_step3.setEnabled(False)

        # File Select
        file_layout = QtWidgets.QHBoxLayout()
        self.input_excel = QtWidgets.QLineEdit()
        self.input_excel.setPlaceholderText("처리할 엑셀 파일 선택")
        btn_browse = QtWidgets.QPushButton("📁 파일 선택")
        btn_browse.clicked.connect(self.browse_excel)
        file_layout.addWidget(self.input_excel)
        file_layout.addWidget(btn_browse)
        step3_layout.addLayout(file_layout)

        # Form fields
        folders_form = QtWidgets.QFormLayout()
        
        self.combo_file_format = QtWidgets.QComboBox()
        self.combo_file_format.addItems([
            "Adobe PDF File(*.pdf)",
            "PNG Image File(*.png)",
            "Microsoft Excel File(*.xlsx)",
            "Microsoft Excel 97-2003 File(*.xls)",
            "Web Page(*.html)",
            "Tab Separated(*.txt)",
            "Comma Separated Values File(*.csv)",
            "Hangul File(*.hwp)",
            "OZ Report Data File(*.ozd)"
        ])
        
        # 입력 방식 라디오 버튼
        self.radio_input_single = QtWidgets.QRadioButton("단일입력 (1명씩)")
        self.radio_input_batch = QtWidgets.QRadioButton("다중입력 (10명 일괄)")
        self.radio_input_single.setChecked(True)
        
        input_mode_layout = QtWidgets.QHBoxLayout()
        input_mode_layout.addWidget(self.radio_input_single)
        input_mode_layout.addWidget(self.radio_input_batch)
        
        pdf_folder_lay = QtWidgets.QHBoxLayout()
        self.input_pdf_folder = QtWidgets.QLineEdit()
        self.input_pdf_folder.setPlaceholderText("PDF 원본 폴더")
        btn_pdf_browse = QtWidgets.QPushButton("📁")
        btn_pdf_browse.clicked.connect(self.browse_pdf_folder)
        pdf_folder_lay.addWidget(self.input_pdf_folder)
        pdf_folder_lay.addWidget(btn_pdf_browse)

        stamped_folder_lay = QtWidgets.QHBoxLayout()
        self.input_pdf_stamped_folder = QtWidgets.QLineEdit()
        self.input_pdf_stamped_folder.setPlaceholderText("서명 완료 폴더")
        btn_stamped_browse = QtWidgets.QPushButton("📁")
        btn_stamped_browse.clicked.connect(self.browse_stamped_folder)
        stamped_folder_lay.addWidget(self.input_pdf_stamped_folder)
        stamped_folder_lay.addWidget(btn_stamped_browse)

        folders_form.addRow("동의서 형식:", self.combo_file_format)
        folders_form.addRow("입력 방식:", input_mode_layout)
        folders_form.addRow("원본 폴더:", pdf_folder_lay)
        folders_form.addRow("완료 폴더:", stamped_folder_lay)
        step3_layout.addLayout(folders_form)

        # Settings
        options_layout = QtWidgets.QHBoxLayout()
        self.check_solting = QtWidgets.QCheckBox("1단계")
        self.check_solting.setChecked(False)
        self.check_insurance = QtWidgets.QCheckBox("2단계")
        self.check_insurance.setChecked(True)
        self.check_stamping = QtWidgets.QCheckBox("🖊️ 스탬프")
        self.check_stamping.setChecked(True)
        self.check_kb_scan = QtWidgets.QCheckBox("4단계")
        self.check_kb_scan.setChecked(False)
        self.check_dry_run = QtWidgets.QCheckBox("Dry-Run")
        
        options_layout.addWidget(self.check_solting)
        options_layout.addWidget(self.check_insurance)
        options_layout.addWidget(self.check_stamping)
        options_layout.addWidget(self.check_kb_scan)
        options_layout.addWidget(self.check_dry_run)
        step3_layout.addLayout(options_layout)

        # PNG 저장 비율 옵션 (KB EDMS 스캔 인식률 향상용 - 확대/축소 비율 160% 이상 권장)
        ratio_layout = QtWidgets.QHBoxLayout()
        self.check_save_ratio = QtWidgets.QCheckBox("📐 PNG 저장 비율")
        self.check_save_ratio.setChecked(False)
        self.check_save_ratio.setToolTip("PNG 저장 시 OZ 뷰어 '확대/축소 비율'을 자동 설정합니다. KB EDMS 스캔 인식률을 위해 160% 이상 권장.")
        self.spin_save_ratio = QtWidgets.QSpinBox()
        self.spin_save_ratio.setRange(100, 400)
        self.spin_save_ratio.setValue(160)
        self.spin_save_ratio.setSuffix(" %")
        self.spin_save_ratio.setMaximumWidth(110)
        self.spin_save_ratio.setEnabled(False)
        self.check_save_ratio.stateChanged.connect(
            lambda s: self.spin_save_ratio.setEnabled(bool(s))
        )
        ratio_layout.addWidget(self.check_save_ratio)
        ratio_layout.addWidget(self.spin_save_ratio)
        ratio_layout.addStretch()
        step3_layout.addLayout(ratio_layout)

        # 50명 단위 자동 폴더링 옵션 (KB 스캔 렉 완화용 - 성공 N명마다 날짜_조번호 하위폴더 분산)
        folder_layout = QtWidgets.QHBoxLayout()
        self.check_auto_folder = QtWidgets.QCheckBox("📁 50명 단위 폴더링")
        self.check_auto_folder.setChecked(False)
        self.check_auto_folder.setToolTip("동의서를 성공 N명 단위로 하위폴더(날짜_조번호)에 자동 분산 저장합니다. KB 스캔 시 렉 완화용.")
        self.spin_folder_interval = QtWidgets.QSpinBox()
        self.spin_folder_interval.setRange(1, 1000)
        self.spin_folder_interval.setValue(50)
        self.spin_folder_interval.setSuffix(" 명")
        self.spin_folder_interval.setMaximumWidth(110)
        self.spin_folder_interval.setEnabled(False)
        self.check_auto_folder.stateChanged.connect(
            lambda s: self.spin_folder_interval.setEnabled(bool(s))
        )
        folder_layout.addWidget(self.check_auto_folder)
        folder_layout.addWidget(self.spin_folder_interval)
        folder_layout.addStretch()
        step3_layout.addLayout(folder_layout)

        # 기등록(로컬) 무시하고 재처리 옵션 — 이 PC가 과거 등록한 고객도 다시 시도
        dedup_layout = QtWidgets.QHBoxLayout()
        self.check_ignore_dedup = QtWidgets.QCheckBox("♻️ 기등록(로컬) 무시하고 재처리")
        self.check_ignore_dedup.setChecked(False)
        self.check_ignore_dedup.setToolTip(
            "이 PC가 과거에 등록 성공한 고객(로컬 저장분)도 건너뛰지 않고 다시 시도합니다.\n"
            "※ 파일 내 중복과 KB 전산 실시간 중복(팝업)은 그대로 유지됩니다.")
        dedup_layout.addWidget(self.check_ignore_dedup)
        dedup_layout.addStretch()
        step3_layout.addLayout(dedup_layout)

        # Triggers
        trigger_layout = QtWidgets.QHBoxLayout()
        self.btn_run = QtWidgets.QPushButton("🚀 깐돌이 자동 등록 시작")
        self.btn_run.setFont(QtGui.QFont("Inter", 10, QtGui.QFont.Weight.Bold))
        self.btn_run.setStyleSheet("background-color: #10b981; color: white; padding: 10px; border-radius: 6px;")
        self.btn_run.clicked.connect(self.start_automation)

        self.btn_stop = QtWidgets.QPushButton("🛑 중단 (Ctrl+D)")
        self.btn_stop.setFont(QtGui.QFont("Inter", 10, QtGui.QFont.Weight.Bold))
        self.btn_stop.setStyleSheet("background-color: #ef4444; color: white; padding: 10px; border-radius: 6px;")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_automation_manually)

        trigger_layout.addWidget(self.btn_run)
        trigger_layout.addWidget(self.btn_stop)
        step3_layout.addLayout(trigger_layout)
        layout.addWidget(self.group_step3)

        # Progress bar
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedHeight(12)
        layout.addWidget(self.progress_bar)

        # Console Logs
        self.console = QtWidgets.QTextBrowser()
        self.console.setMaximumHeight(120)
        layout.addWidget(self.console)

        scroll.setWidget(scroll_widget)
        main_layout.addWidget(scroll)

    def init_edms_tab(self, parent_widget):
        main_layout = QtWidgets.QVBoxLayout(parent_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background-color: transparent; }")

        scroll_widget = QtWidgets.QWidget()
        scroll_widget.setStyleSheet("QWidget { background-color: transparent; }")
        layout = QtWidgets.QVBoxLayout(scroll_widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Folder selection
        folder_layout = QtWidgets.QHBoxLayout()
        self.input_edms_folder = QtWidgets.QLineEdit()
        self.input_edms_folder.setPlaceholderText("EDMS 일괄 업로드할 폴더 선택")
        btn_browse_folder = QtWidgets.QPushButton("📁 폴더 선택")
        btn_browse_folder.clicked.connect(self.browse_edms_folder)
        folder_layout.addWidget(self.input_edms_folder)
        folder_layout.addWidget(btn_browse_folder)
        layout.addLayout(folder_layout)

        # List
        self.list_edms_files = QtWidgets.QListWidget()
        self.list_edms_files.setMaximumHeight(100)
        layout.addWidget(self.list_edms_files)

        # Calibrate Group
        group_calibrate = QtWidgets.QGroupBox("EDMS 좌표 및 화면 자동 보정")
        cal_layout = QtWidgets.QVBoxLayout(group_calibrate)
        
        cal_btn_row = QtWidgets.QHBoxLayout()
        self.btn_edms_calibrate = QtWidgets.QPushButton("🔍 화면 좌표 자동 보정")
        self.btn_edms_calibrate.setStyleSheet("background-color: #3b82f6; color: white; font-weight: bold; padding: 6px;")
        self.btn_edms_calibrate.clicked.connect(self.run_edms_calibration)
        self.btn_edms_save = QtWidgets.QPushButton("💾 설정 저장")
        self.btn_edms_save.setStyleSheet("background-color: #2563eb; color: white; font-weight: bold; padding: 6px;")
        self.btn_edms_save.clicked.connect(self.save_config_to_server)
        cal_btn_row.addWidget(self.btn_edms_calibrate)
        cal_btn_row.addWidget(self.btn_edms_save)
        
        self.lbl_edms_mouse_coords = QtWidgets.QLabel("실시간 마우스 좌표: 추적 중단됨")
        self.lbl_edms_mouse_coords.setFont(QtGui.QFont("Consolas", 9))
        self.check_edms_track_mouse = QtWidgets.QCheckBox("실시간 추적")
        self.check_edms_track_mouse.stateChanged.connect(self.toggle_edms_mouse_tracking)
        
        track_row = QtWidgets.QHBoxLayout()
        track_row.addWidget(self.lbl_edms_mouse_coords)
        track_row.addWidget(self.check_edms_track_mouse)
        
        cal_layout.addLayout(cal_btn_row)
        cal_layout.addLayout(track_row)
        layout.addWidget(group_calibrate)

        # Triggers
        trigger_layout = QtWidgets.QHBoxLayout()
        self.btn_edms_run = QtWidgets.QPushButton("▶️ EDMS 일괄 전송 시작")
        self.btn_edms_run.setFont(QtGui.QFont("Inter", 10, QtGui.QFont.Weight.Bold))
        self.btn_edms_run.setStyleSheet("background-color: #10b981; color: white; padding: 10px; border-radius: 6px;")
        self.btn_edms_run.clicked.connect(self.start_edms_upload)

        self.btn_edms_stop = QtWidgets.QPushButton("🛑 중단 (Ctrl+D)")
        self.btn_edms_stop.setFont(QtGui.QFont("Inter", 10, QtGui.QFont.Weight.Bold))
        self.btn_edms_stop.setStyleSheet("background-color: #ef4444; color: white; padding: 10px; border-radius: 6px;")
        self.btn_edms_stop.setEnabled(False)
        self.btn_edms_stop.clicked.connect(self.stop_edms_upload_manually)

        trigger_layout.addWidget(self.btn_edms_run)
        trigger_layout.addWidget(self.btn_edms_stop)
        layout.addLayout(trigger_layout)

        # Progress bar
        self.progress_bar_edms = QtWidgets.QProgressBar()
        self.progress_bar_edms.setValue(0)
        self.progress_bar_edms.setFixedHeight(12)
        layout.addWidget(self.progress_bar_edms)

        # Console logs
        self.edms_console = QtWidgets.QTextBrowser()
        self.edms_console.setMaximumHeight(120)
        layout.addWidget(self.edms_console)

        scroll.setWidget(scroll_widget)
        main_layout.addWidget(scroll)

    def init_settings_tab(self):
        main_layout = QtWidgets.QVBoxLayout(self.tab_settings)
        main_layout.setContentsMargins(5, 5, 5, 5)
        
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll_widget = QtWidgets.QWidget()
        scroll_layout = QtWidgets.QVBoxLayout(scroll_widget)
        scroll_layout.setSpacing(10)

        # 0. 화면 배율 조절 Group 추가
        zoom_group = QtWidgets.QGroupBox("🔍 화면 스케일 및 폰트 크기 조절")
        zoom_layout = QtWidgets.QVBoxLayout(zoom_group)
        
        lbl_zoom_desc = QtWidgets.QLabel("프로그램 화면이 잘려 보인다면 85% 배율을 선택해 주세요.")
        lbl_zoom_desc.setStyleSheet("color: #94a3b8; font-size: 9pt;")
        zoom_layout.addWidget(lbl_zoom_desc)
        
        btn_row = QtWidgets.QHBoxLayout()
        self.radio_zoom_85 = QtWidgets.QRadioButton("작게 (85%)")
        self.radio_zoom_100 = QtWidgets.QRadioButton("기본 (100%)")
        self.radio_zoom_115 = QtWidgets.QRadioButton("크게 (115%)")
        
        self.radio_zoom_100.setChecked(True)
        btn_row.addWidget(self.radio_zoom_85)
        btn_row.addWidget(self.radio_zoom_100)
        btn_row.addWidget(self.radio_zoom_115)
        zoom_layout.addLayout(btn_row)
        
        self.radio_zoom_85.toggled.connect(lambda checked: checked and self.change_zoom_scale(0.85))
        self.radio_zoom_100.toggled.connect(lambda checked: checked and self.change_zoom_scale(1.0))
        self.radio_zoom_115.toggled.connect(lambda checked: checked and self.change_zoom_scale(1.15))
        
        scroll_layout.addWidget(zoom_group)
        
        # 1. Tracker Group
        track_group = QtWidgets.QGroupBox("📡 실시간 마우스 좌표 추적")
        track_layout = QtWidgets.QHBoxLayout(track_group)
        self.lbl_mouse_coords = QtWidgets.QLabel("마우스 위치 추적: 중지됨 (X: -, Y: -)")
        self.lbl_mouse_coords.setFont(QtGui.QFont("Consolas", 10))
        self.btn_track_mouse = QtWidgets.QPushButton("📡 좌표 추적 시작")
        self.btn_track_mouse.clicked.connect(self.toggle_mouse_tracking)
        track_layout.addWidget(self.lbl_mouse_coords)
        track_layout.addWidget(self.btn_track_mouse)
        scroll_layout.addWidget(track_group)
        
        # 2. Offsets Group
        offsets_group = QtWidgets.QGroupBox("🎯 마우스 클릭 좌표 (오프셋 픽셀)")
        offsets_form = QtWidgets.QFormLayout(offsets_group)
        self.offset_inputs = {}
        default_keys = [
            "image_add_x", "image_add_y", "select_all_x", "select_all_y", "send_x", "send_y",
            "tab_local_pdf_x", "tab_local_pdf_y", "folder_docs_x", "folder_docs_y",
            "search_input_x", "search_input_y", "search_btn_x", "search_btn_y",
            "confirm_btn_x", "confirm_btn_y", "pop_send_btn_x", "pop_send_btn_y",
            "fallback_pop_send_x", "fallback_pop_send_y"
        ]
        for key in default_keys:
            spin = QtWidgets.QSpinBox()
            spin.setRange(0, 10000)
            spin.setValue(0)
            self.offset_inputs[key] = spin
            korean_label = OFFSET_LABELS.get(key, key)
            offsets_form.addRow(f"{korean_label}:", spin)
        scroll_layout.addWidget(offsets_group)
        
        # 3. Delays Group
        delays_group = QtWidgets.QGroupBox("⏳ 딜레이 설정 (초 단위)")
        delays_form = QtWidgets.QFormLayout(delays_group)
        self.delay_inputs = {}
        default_delay_keys = [
            "dialog_open_wait", "tab_click_wait", "folder_expand_wait", "search_wait",
            "image_load_wait", "select_all_wait", "send_confirm_wait", "success_alert_wait"
        ]
        for key in default_delay_keys:
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(0.0, 120.0)
            spin.setSingleStep(0.1)
            spin.setValue(1.0)
            self.delay_inputs[key] = spin
            korean_label = DELAY_LABELS.get(key, key)
            delays_form.addRow(f"{korean_label}:", spin)
        scroll_layout.addWidget(delays_group)
        
        scroll.setWidget(scroll_widget)
        main_layout.addWidget(scroll)
        
        # 4. Sync Buttons
        sync_layout = QtWidgets.QHBoxLayout()
        btn_load = QtWidgets.QPushButton("⬇️ 서버에서 불러오기")
        btn_load.clicked.connect(self.load_config_from_server)
        self.btn_save_server = QtWidgets.QPushButton("⬆️ 서버에 업로드")
        self.btn_save_server.clicked.connect(self.save_config_to_server)
        sync_layout.addWidget(btn_load)
        sync_layout.addWidget(self.btn_save_server)
        main_layout.addLayout(sync_layout)

    def init_guide_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_guide)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(10)

        # Sub-tab widget for guides
        self.guide_tabs = QtWidgets.QTabWidget()
        self.guide_tabs.setStyleSheet("""
            QTabWidget::pane {
                border: 1px solid #334155;
                background-color: #1e293b;
                border-radius: 8px;
            }
            QTabBar::tab {
                background-color: #0f172a;
                color: #94a3b8;
                border: 1px solid #1e293b;
                padding: 8px 16px;
            }
            QTabBar::tab:selected {
                background-color: #1e293b;
                color: #38bdf8;
                border-color: #334155;
            }
        """)

        # 1. User Manual Tab
        tab_manual = QtWidgets.QWidget()
        layout_manual = QtWidgets.QVBoxLayout(tab_manual)
        self.txt_manual = QtWidgets.QTextBrowser()
        self.txt_manual.setStyleSheet("background-color: #1e293b; color: white; border: none; font-family: 'Malgun Gothic'; font-size: 10pt;")
        layout_manual.addWidget(self.txt_manual)
        self.guide_tabs.addTab(tab_manual, "📋 사용설명서")

        # 2. Setup Guide Tab
        tab_setup = QtWidgets.QWidget()
        layout_setup = QtWidgets.QVBoxLayout(tab_setup)
        self.txt_setup = QtWidgets.QTextBrowser()
        self.txt_setup.setStyleSheet("background-color: #1e293b; color: white; border: none; font-family: 'Malgun Gothic'; font-size: 10pt;")
        layout_setup.addWidget(self.txt_setup)
        self.guide_tabs.addTab(tab_setup, "⚙️ 사전세팅 가이드")

        layout.addWidget(self.guide_tabs)
        self.load_guides()

    def load_guides(self):
        import sys
        from pathlib import Path
        
        # Determine base path (support both development and PyInstaller frozen modes)
        if hasattr(sys, "_MEIPASS"):
            base_dir = Path(sys._MEIPASS)
        else:
            base_dir = Path(__file__).parent.parent

        paths_manual = [
            base_dir / "docs" / "사용설명서.md",
            Path("docs/사용설명서.md"),
            Path("사용설명서.md"),
        ]
        paths_setup = [
            base_dir / "docs" / "사전세팅가이드.md",
            Path("docs/사전세팅가이드.md"),
            Path("사전세팅가이드.md"),
        ]

        manual_content = "사용설명서 파일을 찾을 수 없습니다."
        for p in paths_manual:
            if p and p.exists():
                try:
                    manual_content = p.read_text(encoding="utf-8")
                    break
                except Exception:
                    pass

        setup_content = "사전세팅가이드 파일을 찾을 수 없습니다."
        for p in paths_setup:
            if p and p.exists():
                try:
                    setup_content = p.read_text(encoding="utf-8")
                    break
                except Exception:
                    pass

        self.txt_manual.setMarkdown(manual_content)
        self.txt_setup.setMarkdown(setup_content)

    def connect_supabase(self):
        if not SUPABASE_URL or not SUPABASE_KEY:
            QtWidgets.QMessageBox.critical(self, "오류", ".env 파일에 Supabase 설정 정보가 누락되었습니다.")
            return
        try:
            self.supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "오류", f"Supabase 연결 실패: {e}")

    # --- Verification & Login logic ---
    def check_device_status_on_startup(self):
        if not self.supabase:
            return

        self.lbl_auth_status.setText("")
        try:
            res = self.supabase.rpc("get_device_status", {"p_hwid": self.hwid}).execute()
            status_data = res.data

            if not status_data or not status_data.get("registered"):
                self.show_register_ui()
                return

            self.device = {
                "id": status_data.get("id"),
                "device_name": status_data.get("device_name"),
                "status": status_data.get("status"),
                "pin_code": status_data.get("pin_code"),
                "tenant_name": status_data.get("tenant_name")
            }

            if self.device["status"] == "blocked":
                self.show_blocked_ui()
                return

            if self.device["status"] == "pending":
                self.show_pending_ui()
                return

            # Approved
            # Check if there is cached local PIN
            saved_pin = self.load_local_pin()
            if saved_pin:
                # Try auto-login
                try:
                    verify_res = self.supabase.rpc("verify_device_pin", {"p_pin_code": saved_pin, "p_hwid": self.hwid}).execute()
                    if verify_res.data:
                        data = verify_res.data
                        self.device = data["device"]
                        self.tenant = data["tenant"]
                        if data.get("config"):
                            self.apply_server_config(data["config"])
                        self.on_login_success()
                        return
                except Exception:
                    pass

            # Otherwise show PIN input
            self.show_pin_login_ui()

        except Exception as e:
            self.lbl_auth_status.setText(f"기기 로딩 실패: {e}")
            self.show_pin_login_ui()

    def show_register_ui(self):
        self.auth_stack.setCurrentIndex(0)
        self.disable_all_tabs()
        self.load_tenant_list()

    def load_tenant_list(self):
        try:
            res = self.supabase.rpc("get_tenant_list", {}).execute()
            if res.data:
                self.combo_tenant.clear()
                self.tenants_list = res.data
                for t in self.tenants_list:
                    self.combo_tenant.addItem(t["name"])
        except Exception as e:
            self.lbl_auth_status.setText(f"회사 목록 로딩 실패: {e}")

    def submit_registration(self):
        if not self.tenants_list:
            QtWidgets.QMessageBox.warning(self, "경고", "회사 목록을 불러오는 중입니다. 잠시 후 다시 시도해 주세요.")
            return

        tenant_idx = self.combo_tenant.currentIndex()
        if tenant_idx < 0:
            QtWidgets.QMessageBox.warning(self, "경고", "소속 회사를 선택해 주세요.")
            return

        tenant_id = self.tenants_list[tenant_idx]["id"]
        device_name = self.input_device_name.text().strip()
        if not device_name:
            QtWidgets.QMessageBox.warning(self, "경고", "기기 명칭을 입력해 주세요.")
            return

        self.btn_register_device.setEnabled(False)
        self.btn_register_device.setText("신청 등록 중...")

        try:
            res = self.supabase.rpc("register_device_via_client", {
                "p_tenant_id": tenant_id,
                "p_hwid": self.hwid,
                "p_device_name": device_name
            }).execute()

            if res.data and res.data.get("success"):
                QtWidgets.QMessageBox.information(self, "신청 완료", "기기 등록 신청을 보냈어요!\n관리자가 승인하면 6자리 PIN 인증번호가 발급됩니다.")
                self.check_device_status_on_startup()
            else:
                raise ValueError("서버 응답 오류")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "오류", f"기기 등록 신청 실패: {e}")
        finally:
            self.btn_register_device.setEnabled(True)
            self.btn_register_device.setText("등록 신청하기")

    def show_pending_ui(self):
        self.auth_stack.setCurrentIndex(1)
        self.disable_all_tabs()
        self.lbl_pen_desc.setText(f"회사명: {self.device.get('tenant_name')}\n기기명: {self.device.get('device_name')}\n\n관리자 승인 후 발급받은 6자리 PIN 번호를 아래에 입력해 주세요.")

    def login_via_pending_pin(self):
        pin = self.input_pin_pending.text().strip()
        if len(pin) != 6 or not pin.isdigit():
            QtWidgets.QMessageBox.warning(self, "경고", "올바른 6자리 숫자를 입력해 주세요.")
            return
        self.verify_pin_and_login(pin)

    def show_pin_login_ui(self):
        self.auth_stack.setCurrentIndex(2)
        self.disable_all_tabs()

    def login_via_submit_pin(self):
        pin = self.input_pin_login.text().strip()
        if len(pin) != 6 or not pin.isdigit():
            QtWidgets.QMessageBox.warning(self, "경고", "올바른 6자리 숫자를 입력해 주세요.")
            return
        self.verify_pin_and_login(pin)

    def verify_pin_and_login(self, pin):
        try:
            res = self.supabase.rpc("verify_device_pin", {"p_pin_code": pin, "p_hwid": self.hwid}).execute()
            if not res.data:
                raise ValueError("올바르지 않은 응답 데이터입니다.")

            data = res.data
            self.device = data["device"]
            self.tenant = data["tenant"]

            # Save local pin
            self.save_local_pin(pin)

            # Apply macro configs
            if data.get("config"):
                self.apply_server_config(data["config"])

            self.on_login_success()
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "인증 실패", f"인증번호가 일치하지 않거나 오류가 발생했어요:\n{e}")

    def show_blocked_ui(self):
        self.disable_all_tabs()
        self.lbl_auth_status.setText("🚫 차단된 기기입니다. 관리자에게 문의해 주세요.")
        QtWidgets.QMessageBox.critical(self, "기기 차단됨", "이 기기는 차단 상태입니다. 전산 등록 시스템을 구동할 수 없습니다.")

    def on_login_success(self):
        self.auth_stack.setCurrentIndex(3)
        self.lbl_auth_status.setText("")
        
        info = (
            f"• 소속 회사: {self.tenant['name']}\n"
            f"• 기기 명칭: {self.device['device_name']}\n"
            f"• 인증 상태: 승인 완료 및 연동 성공\n"
            f"• 고유 식별자(HWID): {self.hwid}"
        )
        self.info_browser.setText(info)
        
        # Enable all tabs
        self.tabs.setTabEnabled(1, True)
        self.tabs.setTabEnabled(2, True)
        self.tabs.setTabEnabled(3, True)  # 고객DB 수집
        self.tabs.setCurrentIndex(1) # Auto jump to Solting automation

        # Start heartbeats
        self.send_heartbeat()
        self.heartbeat_timer.start(30000)

    def logout_device(self):
        self.delete_local_pin()
        self.device = None
        self.tenant = None
        self.heartbeat_timer.stop()
        self.check_device_status_on_startup()

    def disable_all_tabs(self):
        self.tabs.setTabEnabled(1, False)
        self.tabs.setTabEnabled(2, False)
        self.tabs.setTabEnabled(3, False)

    def load_local_pin(self):
        pin_file = APP_DIR / ".pin"
        if pin_file.exists():
            try:
                return pin_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        return None

    def save_local_pin(self, pin):
        try:
            (APP_DIR / ".pin").write_text(pin, encoding="utf-8")
        except Exception:
            pass

    def delete_local_pin(self):
        try:
            pin_file = APP_DIR / ".pin"
            if pin_file.exists():
                pin_file.unlink()
        except Exception:
            pass

    def send_heartbeat(self):
        if self.supabase and self.device and self.device.get("id"):
            try:
                self.supabase.rpc("heartbeat_device_via_pin", {"p_device_id": self.device["id"]}).execute()
            except Exception:
                pass

    def apply_server_config(self, config_data):
        offsets = config_data.get("offsets", {})
        delays = config_data.get("delays", {})

        for k, spin in self.offset_inputs.items():
            if k in offsets:
                spin.setValue(int(offsets[k]))

        for k, spin in self.delay_inputs.items():
            if k in delays:
                spin.setValue(float(delays[k]))

        self.log_message("[설정] 서버에서 최신 매크로 설정을 동기화했어요.")

    # --- Configuration syncing ---
    def load_config_from_server(self):
        if not self.tenant:
            return
        try:
            res = self.supabase.rpc("get_macro_config_via_device", {"p_tenant_id": self.tenant["id"]}).execute()
            if res.data:
                self.apply_server_config(res.data)
                QtWidgets.QMessageBox.information(self, "성공", "서버에서 최신 매크로 설정을 동기화했어요.")
            else:
                self.log_message("[안내] 서버에 저장된 회사 매크로 설정이 없어 기본값을 사용합니다.")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "오류", f"설정 로드 실패: {e}")

    def save_config_to_server(self):
        if not self.tenant:
            return
        
        offsets = {k: spin.value() for k, spin in self.offset_inputs.items()}
        delays = {k: spin.value() for k, spin in self.delay_inputs.items()}

        # [좌표 반영 버그 수정] 매크로(batch_upload_via_win32)는 edms_config.json 을 읽으므로,
        # 서버뿐 아니라 로컬 edms_config.json 에도 반드시 함께 저장해야 설정이 매크로에 반영된다.
        self._write_edms_config_json(offsets, delays)

        try:
            self.supabase.rpc("save_macro_config_via_device", {
                "p_tenant_id": self.tenant["id"],
                "p_offsets": offsets,
                "p_delays": delays
            }).execute()
            QtWidgets.QMessageBox.information(self, "성공", "매크로 설정을 로컬(edms_config.json)과 서버에 저장 완료!")
            self.log_message("[설정] 매크로 좌표/딜레이를 edms_config.json 및 서버에 저장 완료했어요.")
        except Exception as e:
            # 서버 저장 실패해도 로컬 저장은 이미 됐으므로 매크로엔 반영됨
            QtWidgets.QMessageBox.warning(self, "부분 저장", f"로컬(edms_config.json)에는 저장됐으나 서버 저장은 실패했어요:\n{e}")
            self.log_message(f"[설정] 로컬 저장 완료, 서버 저장 실패: {e}")

    def _write_edms_config_json(self, offsets: dict, delays: dict):
        """매크로가 읽는 edms_config.json 에 좌표/딜레이를 병합 저장한다.
        오프셋 0(미설정 스핀박스)은 기존 값을 보존하기 위해 덮어쓰지 않는다."""
        import json
        try:
            edms_path = APP_DIR / "edms_config.json"
            data = {}
            if edms_path.exists():
                with open(edms_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            cur_off = data.get("offsets", {}) or {}
            for k, v in offsets.items():
                if v not in (0, None):   # 0 = 미설정으로 간주, 기존값 유지
                    cur_off[k] = v
            data["offsets"] = cur_off
            data["delays"] = dict(delays)
            data.setdefault("ratios", {"pop_send_x": 0.693989, "pop_send_y": 0.873817})
            with open(edms_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            self.log_message(f"[설정] edms_config.json 갱신 완료: {edms_path}")
        except Exception as e:
            self.log_message(f"[설정] edms_config.json 저장 실패: {e}")

    def _load_edms_config_into_spins(self):
        """매크로가 읽는 edms_config.json 의 offsets/delays 를 설정 탭 스핀박스에 반영한다."""
        import json
        try:
            if not hasattr(self, "offset_inputs"):
                return
            edms_path = APP_DIR / "edms_config.json"
            if not edms_path.exists():
                return
            with open(edms_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in (data.get("offsets") or {}).items():
                if k in self.offset_inputs and v not in (None, ""):
                    self.offset_inputs[k].setValue(int(v))
            for k, v in (data.get("delays") or {}).items():
                if k in self.delay_inputs and v not in (None, ""):
                    self.delay_inputs[k].setValue(float(v))
        except Exception:
            pass

    # --- Mouse coordinate tracker ---
    def toggle_mouse_tracking(self):
        if self.mouse_tracker.isRunning():
            self.mouse_tracker.stop()
            self.btn_track_mouse.setText("📡 마우스 좌표 추적 시작")
            self.lbl_mouse_coords.setText("마우스 위치 추적: 중지됨 (X: -, Y: -)")
        else:
            self.mouse_tracker.start()
            self.btn_track_mouse.setText("📡 마우스 좌표 추적 중지")

    def update_mouse_pos(self, x, y):
        self.lbl_mouse_coords.setText(f"마우스 위치 추적 중: X: {x}, Y: {y}")
        self.lbl_edms_mouse_coords.setText(f"실시간 마우스 좌표: X: {x}, Y: {y}")

    # --- File browsers ---
    def browse_excel(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "엑셀 파일 선택", "", "Excel Files (*.xlsx *.xls)")
        if file_path:
            self.input_excel.setText(file_path)

    def browse_pdf_folder(self):
        dir_path = QtWidgets.QFileDialog.getExistingDirectory(self, "PDF 원본 저장 폴더 선택")
        if dir_path:
            self.input_pdf_folder.setText(dir_path)

    def browse_stamped_folder(self):
        dir_path = QtWidgets.QFileDialog.getExistingDirectory(self, "서명 완료 폴더 선택")
        if dir_path:
            self.input_pdf_stamped_folder.setText(dir_path)

    def browse_edms_folder(self):
        dir_path = QtWidgets.QFileDialog.getExistingDirectory(self, "EDMS 업로드할 폴더 선택")
        if dir_path:
            self.input_edms_folder.setText(dir_path)
            self.scan_edms_folder(dir_path)

    def scan_edms_folder(self, folder_path):
        self.list_edms_files.clear()
        self.edms_pdf_paths = []
        folder = Path(folder_path)
        if folder.exists() and folder.is_dir():
            files = []
            for ext in ["*.pdf", "*.png"]:
                files.extend(folder.glob(ext))
            files = sorted(files, key=lambda p: p.name)
            for p in files:
                self.list_edms_files.addItem(f"📄 {p.name} ({p.stat().st_size // 1024} KB)")
                self.edms_pdf_paths.append(str(p.resolve()))
            self.log_edms_message(f"[스캔] {len(self.edms_pdf_paths)}개의 파일을 감지했어요.")

    # --- Step 1 & 2 Logic ---
    def open_edge_browser(self):
        self.btn_open_edge.setEnabled(False)
        self.btn_open_edge.setText("Edge 기동 중...")
        try:
            cmd = 'start "" msedge.exe --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\\kb-edge-debug" "https://nsales.kbinsure.co.kr/eus/ch/ch_index.jsp"'
            subprocess.Popen(cmd, shell=True)
            self.log_message("[알림] Edge 브라우저를 디버그 포트 9222로 기동했습니다. (브라우저 창을 닫지 마세요)")
            
            # 1단계 성공 -> 2단계 잠금 해제
            self.group_step2.setEnabled(True)
            self.log_message("[안내] 1단계가 완료되었어요. 2단계 포털 자동 로그인을 진행해 주세요! 🔑")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "오류", f"Edge 브라우저 기동 실패: {e}")
        finally:
            self.btn_open_edge.setEnabled(True)
            self.btn_open_edge.setText("🌐 Edge 브라우저 기동")

    def run_auto_login(self):
        username = self.input_login_id.text().strip()
        password = self.input_login_pw.text().strip()
        birthdate = self.input_login_birth.text().strip()

        if not username or not password or not birthdate:
            QtWidgets.QMessageBox.warning(self, "경고", "아이디, 비밀번호, 생년월일을 모두 입력해 주세요.")
            return

        self.btn_auto_login.setEnabled(False)
        self.btn_auto_login.setText("자동 로그인 수행 중...")
        self.log_message("[알림] 포털 자동 로그인을 수행하는 중입니다. 잠시만 기다려 주세요...")
        
        self.login_worker = LoginWorker(username, password, birthdate)
        self.login_worker.finished_signal.connect(self.on_login_finished)
        self.login_worker.start()

    def on_login_finished(self, success, msg):
        self.btn_auto_login.setEnabled(True)
        self.btn_auto_login.setText("🔑 자동 로그인 실행")
        if success:
            if self.check_save_credentials.isChecked():
                self.save_credentials()
            else:
                self.clear_credentials()
            
            # 2단계 성공 -> 3단계 잠금 해제
            self.group_step3.setEnabled(True)
            QtWidgets.QMessageBox.information(self, "성공", msg)
            self.log_message("[알림] 포털 자동 로그인에 성공했습니다.")
            self.log_message("[안내] 2단계가 완료되었어요. 3단계 파일 및 저장 폴더 설정을 마친 후 [깐돌이 자동 등록 시작]을 눌러주세요! 🚀")
        else:
            QtWidgets.QMessageBox.critical(self, "오류", msg)
            self.log_message(f"[오류] 자동 로그인 실패: {msg}")

    # --- EDMS Calibration & Tracker Logic ---
    def run_edms_calibration(self):
        self.btn_edms_calibrate.setEnabled(False)
        self.btn_edms_calibrate.setText("보정 계산 중...")
        self.log_edms_message("[보정] 로컬 웹 서버 API를 호출하여 EDMS 화면 좌표 자동 보정을 진행합니다...")
        
        self.cal_worker = CalibrateWorker()
        self.cal_worker.finished_signal.connect(self.on_calibration_finished)
        self.cal_worker.start()

    def on_calibration_finished(self, success, detected, err_msg):
        self.btn_edms_calibrate.setEnabled(True)
        self.btn_edms_calibrate.setText("🔍 EDMS 화면 좌표 자동 보정")
        
        if success:
            updated_count = 0
            for k, v in detected.items():
                if k in self.offset_inputs:
                    self.offset_inputs[k].setValue(int(v))
                    updated_count += 1
            if updated_count > 0:
                QtWidgets.QMessageBox.information(
                    self, "성공", 
                    f"자동 보정 성공! {updated_count}개의 주요 좌표를 갱신했습니다.\n설정을 영구 반영하려면 [💾 설정 저장] 또는 [⬆️ 서버에 업로드] 버튼을 꼭 눌러주세요."
                )
                self.log_edms_message(f"[보정] {updated_count}개의 주요 좌표 보정값을 갱신했습니다.")
            else:
                QtWidgets.QMessageBox.warning(self, "안내", "분석은 완료했으나 유효한 버튼 좌표를 검출하지 못했습니다.")
                self.log_edms_message("[보정] 유효한 버튼 좌표 검출 실패")
        else:
            QtWidgets.QMessageBox.critical(self, "보정 실패", err_msg)
            self.log_edms_message(f"[오류] 자동 보정 실패: {err_msg}")

    def toggle_edms_mouse_tracking(self, state):
        if state == 2: # Checked
            self.lbl_edms_mouse_coords.setText("추적 중...")
            if not self.mouse_tracker.isRunning():
                self.mouse_tracker.start()
                self.btn_track_mouse.setText("📡 마우스 좌표 추적 중지")
        else: # Unchecked
            self.lbl_edms_mouse_coords.setText("실시간 마우스 좌표: 추적 중단됨")
            if self.btn_track_mouse.text() == "📡 마우스 좌표 추적 시작":
                if self.mouse_tracker.isRunning():
                    self.mouse_tracker.stop()

    # ── 고객DB 수집 탭 (KB 보장분석 수집) ─────────────────────────────
    def init_customerdb_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_customerdb)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        title = QtWidgets.QLabel("🗂️ KB 보장분석 고객DB 수집")
        title.setFont(QtGui.QFont("Malgun Gothic", 14, QtGui.QFont.Weight.Bold))
        title.setStyleSheet("color: white;")
        layout.addWidget(title)

        desc = QtWidgets.QLabel(
            "KB전산에 로그인된 본인 세션의 '보장분석' 화면에 표시되는 담당 고객 데이터를 수집해\n"
            "어드민 서버의 '고객DB' 메뉴에 정리합니다. (고객명·생년월일·나이·성별·월보험료·가입건수·동의종료일 등)"
        )
        desc.setStyleSheet("color: #94a3b8; font-size: 10pt;")
        desc.setWordWrap(True)
        layout.addWidget(desc)

        notice = QtWidgets.QLabel(
            "⚠️ 수집 대상은 개인신용정보입니다. 본인 담당 고객에 한해 신용정보법·개인정보보호법을 준수하여 사용하세요."
        )
        notice.setStyleSheet("color: #f59e0b; font-size: 9pt; background:#1e293b; border:1px solid #334155; border-radius:6px; padding:6px;")
        notice.setWordWrap(True)
        layout.addWidget(notice)

        guide = QtWidgets.QLabel(
            "사용법: ① KB전산 보장분석 화면을 열고 '조회'로 고객 목록을 띄웁니다 →\n"
            "② (선택) 아래에서 동의서 진행 엑셀을 골라 전화번호를 매칭 →\n"
            "③ '고객DB 수집 시작'을 누르면 화면 데이터를 자동 수집·저장합니다."
        )
        guide.setStyleSheet("color: #64748b; font-size: 9pt;")
        guide.setWordWrap(True)
        layout.addWidget(guide)

        # 전화번호 매칭용 엑셀 선택 (선택 사항)
        self.crawl_contact_paths = []
        contact_row = QtWidgets.QHBoxLayout()
        self.btn_pick_contacts = QtWidgets.QPushButton("📎 전화번호 매칭 엑셀 선택(선택)")
        self.btn_pick_contacts.setStyleSheet("background-color: #334155; color: #e2e8f0; border-radius: 6px; padding: 6px;")
        self.btn_pick_contacts.clicked.connect(self.pick_contact_excels)
        self.lbl_contacts = QtWidgets.QLabel("선택된 엑셀 없음 (전화번호 매칭 안 함)")
        self.lbl_contacts.setStyleSheet("color: #94a3b8; font-size: 9pt;")
        self.btn_clear_contacts = QtWidgets.QPushButton("초기화")
        self.btn_clear_contacts.setStyleSheet("background-color: #1e293b; color: #94a3b8; border-radius: 6px; padding: 6px;")
        self.btn_clear_contacts.clicked.connect(self.clear_contact_excels)
        contact_row.addWidget(self.btn_pick_contacts, 2)
        contact_row.addWidget(self.lbl_contacts, 3)
        contact_row.addWidget(self.btn_clear_contacts, 1)
        layout.addLayout(contact_row)

        # 버튼 줄
        btn_row = QtWidgets.QHBoxLayout()
        self.btn_crawl_start = QtWidgets.QPushButton("▶️ 고객DB 수집 시작")
        self.btn_crawl_start.setFixedHeight(40)
        self.btn_crawl_start.setStyleSheet("background-color: #2563eb; color: white; border-radius: 8px; font-weight: bold;")
        self.btn_crawl_start.clicked.connect(self.start_customer_crawl)

        self.btn_crawl_stop = QtWidgets.QPushButton("🛑 중단")
        self.btn_crawl_stop.setFixedHeight(40)
        self.btn_crawl_stop.setStyleSheet("background-color: #ef4444; color: white; border-radius: 8px;")
        self.btn_crawl_stop.setEnabled(False)
        self.btn_crawl_stop.clicked.connect(self.stop_customer_crawl)

        btn_row.addWidget(self.btn_crawl_start, 3)
        btn_row.addWidget(self.btn_crawl_stop, 1)
        layout.addLayout(btn_row)

        # 진행률
        self.crawl_progress = QtWidgets.QProgressBar()
        self.crawl_progress.setValue(0)
        layout.addWidget(self.crawl_progress)

        # 결과 미리보기 표 (한국어 헤더)
        self.crawl_headers = ["고객명", "생년월일", "전화번호", "나이", "성별", "월보험료", "가입건수", "동의종료일", "분석일자"]
        self.crawl_table = QtWidgets.QTableWidget(0, len(self.crawl_headers))
        self.crawl_table.setHorizontalHeaderLabels(self.crawl_headers)
        self.crawl_table.horizontalHeader().setStretchLastSection(True)
        self.crawl_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.crawl_table.setStyleSheet("background:#0f172a; color:#e2e8f0; gridline-color:#334155;")
        self.crawl_table.setMinimumHeight(180)
        layout.addWidget(self.crawl_table, 1)

        # 로그 콘솔
        self.crawl_console = QtWidgets.QTextEdit()
        self.crawl_console.setReadOnly(True)
        self.crawl_console.setMaximumHeight(140)
        self.crawl_console.setStyleSheet("background:#0b1220; color:#9ca3af; font-family:Consolas; font-size:9pt;")
        layout.addWidget(self.crawl_console)

        self.customer_crawl_worker = None

    def start_customer_crawl(self):
        if not (self.tenant and self.device):
            QtWidgets.QMessageBox.warning(self, "인증 필요", "먼저 기기 인증(로그인)을 완료해 주세요.")
            return
        try:
            out_dir = APP_DIR / "output"
            out_dir.mkdir(parents=True, exist_ok=True)
            dump_path = str(out_dir / "bojang_capture.log")
        except Exception:
            dump_path = None

        self.btn_crawl_start.setEnabled(False)
        self.btn_crawl_start.setText("수집 중...")
        self.btn_crawl_stop.setEnabled(True)
        self.crawl_console.clear()
        self.crawl_table.setRowCount(0)
        self.crawl_progress.setValue(0)

        self.customer_crawl_worker = CustomerCrawlWorker(
            "http://localhost:9222",
            self.tenant["id"],
            self.device["id"],
            self.supabase,
            dump_path,
            list(self.crawl_contact_paths),
        )
        self.customer_crawl_worker.log_signal.connect(self._crawl_log)
        self.customer_crawl_worker.progress_signal.connect(self._crawl_progress)
        self.customer_crawl_worker.rows_signal.connect(self._crawl_fill_table)
        self.customer_crawl_worker.finished_signal.connect(self._crawl_finished)
        self.customer_crawl_worker.start()

    def pick_contact_excels(self):
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "전화번호 매칭용 동의서 엑셀 선택", "", "Excel 파일 (*.xlsx *.xls)")
        if paths:
            self.crawl_contact_paths = list(paths)
            self.lbl_contacts.setText(f"엑셀 {len(paths)}개 선택됨 → 전화번호 매칭 적용")
            self.lbl_contacts.setStyleSheet("color: #34d399; font-size: 9pt;")

    def clear_contact_excels(self):
        self.crawl_contact_paths = []
        self.lbl_contacts.setText("선택된 엑셀 없음 (전화번호 매칭 안 함)")
        self.lbl_contacts.setStyleSheet("color: #94a3b8; font-size: 9pt;")

    def stop_customer_crawl(self):
        if self.customer_crawl_worker and self.customer_crawl_worker.isRunning():
            self.btn_crawl_stop.setEnabled(False)
            self.btn_crawl_stop.setText("중단 중...")
            self.customer_crawl_worker.stop_requested = True

    def _crawl_log(self, msg):
        self.crawl_console.append(msg)
        self.crawl_console.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def _crawl_progress(self, done, total, msg):
        self.crawl_progress.setMaximum(max(total, 1))
        self.crawl_progress.setValue(done)
        if msg:
            self._crawl_log(f"[진행] {done}/{total} - {msg}")

    def _crawl_fill_table(self, rows):
        keys = ["customer_name", "birth", "phone", "age", "gender", "monthly_premium",
                "policy_count", "consent_end_date", "analysis_date"]
        self.crawl_table.setRowCount(len(rows))
        for r, rec in enumerate(rows):
            for c, k in enumerate(keys):
                val = rec.get(k)
                self.crawl_table.setItem(r, c, QtWidgets.QTableWidgetItem("" if val is None else str(val)))

    def _crawl_finished(self, success, msg, count):
        self.btn_crawl_start.setEnabled(True)
        self.btn_crawl_start.setText("▶️ 고객DB 수집 시작")
        self.btn_crawl_stop.setEnabled(False)
        self.btn_crawl_stop.setText("🛑 중단")
        if success:
            QtWidgets.QMessageBox.information(self, "수집 완료", msg)
        else:
            QtWidgets.QMessageBox.warning(self, "수집 안내", msg)
        self.customer_crawl_worker = None

    def load_initial_values(self):
        # [좌표 반영 버그 수정] 매크로가 읽는 edms_config.json 의 좌표/딜레이를 설정 탭 스핀박스에 로드해
        # UI 가 매크로의 실제 적용값을 그대로 보여주도록 한다(설정 탭 = 매크로 단일 출처).
        self._load_edms_config_into_spins()
        try:
            from solting_auto.config import load_config
            cfg = load_config(str(APP_DIR / "config.yaml"))

            pdf_folder = cfg.get("insurance", {}).get("pdf_folder", "./output/consent_pdfs")
            pdf_stamped_folder = cfg.get("insurance", {}).get("pdf_stamped_folder", "./output/consent_pdfs_stamped")
            stamping_enabled = cfg.get("insurance", {}).get("stamping_enabled", True)
            file_format = cfg.get("insurance", {}).get("oz", {}).get("file_format", "PDF")
            
            # UI Zoom 로드 및 적용
            ui_cfg = cfg.get("ui", {})
            zoom = ui_cfg.get("zoom", 1.0)
            
            self.radio_zoom_85.blockSignals(True)
            self.radio_zoom_100.blockSignals(True)
            self.radio_zoom_115.blockSignals(True)
            
            if zoom == 0.85:
                self.radio_zoom_85.setChecked(True)
            elif zoom == 1.15:
                self.radio_zoom_115.setChecked(True)
            else:
                self.radio_zoom_100.setChecked(True)
                
            self.radio_zoom_85.blockSignals(False)
            self.radio_zoom_100.blockSignals(False)
            self.radio_zoom_115.blockSignals(False)
            
            self.update_global_stylesheet(zoom)
            
            self.input_pdf_folder.setText(str(Path(pdf_folder).resolve()))
            self.input_pdf_stamped_folder.setText(str(Path(pdf_stamped_folder).resolve()))
            self.check_stamping.setChecked(stamping_enabled)
            kb_scan_enabled = cfg.get("insurance", {}).get("kb_scan_enabled", False)
            self.check_kb_scan.setChecked(kb_scan_enabled)

            # PNG 저장 비율 옵션 로드
            _oz = cfg.get("insurance", {}).get("oz", {})
            self.check_save_ratio.setChecked(bool(_oz.get("save_ratio_enabled", False)))
            self.spin_save_ratio.setValue(int(_oz.get("save_ratio", 160)))
            self.spin_save_ratio.setEnabled(self.check_save_ratio.isChecked())

            # 50명 단위 자동 폴더링 옵션 로드
            _ins = cfg.get("insurance", {})
            self.check_auto_folder.setChecked(bool(_ins.get("auto_folder_enabled", False)))
            self.spin_folder_interval.setValue(int(_ins.get("auto_folder_interval", 50)))
            self.spin_folder_interval.setEnabled(self.check_auto_folder.isChecked())

            # 입력 방식 로드
            input_mode = cfg.get("insurance", {}).get("input_mode", "single")
            if input_mode == "batch":
                self.radio_input_batch.setChecked(True)
            else:
                self.radio_input_single.setChecked(True)
            
            shorthand_map = {
                "PDF": "Adobe PDF File(*.pdf)",
                "Adobe PDF File(*.pdf)": "Adobe PDF File(*.pdf)",
                "PNG": "PNG Image File(*.png)",
                "PNG Image File(*.png)": "PNG Image File(*.png)",
                "Excel": "Microsoft Excel File(*.xlsx)",
                "Microsoft Excel File(*.xlsx)": "Microsoft Excel File(*.xlsx)",
                "xls": "Microsoft Excel 97-2003 File(*.xls)",
                "Microsoft Excel 97-2003 File(*.xls)": "Microsoft Excel 97-2003 File(*.xls)",
                "HTML": "Web Page(*.html)",
                "Web Page(*.html)": "Web Page(*.html)",
                "TXT": "Tab Separated(*.txt)",
                "Tab Separated(*.txt)": "Tab Separated(*.txt)",
                "CSV": "Comma Separated Values File(*.csv)",
                "Comma Separated Values File(*.csv)": "Comma Separated Values File(*.csv)",
                "HWP": "Hangul File(*.hwp)",
                "Hangul File(*.hwp)": "Hangul File(*.hwp)",
                "OZD": "OZ Report Data File(*.ozd)",
                "OZ Report Data File(*.ozd)": "OZ Report Data File(*.ozd)"
            }
            mapped_format = shorthand_map.get(file_format, "Adobe PDF File(*.pdf)")
            idx = self.combo_file_format.findText(mapped_format)
            if idx >= 0:
                self.combo_file_format.setCurrentIndex(idx)
        except Exception as e:
            print(f"초기 값 로드 실패: {e}")
            
        self.load_credentials()

    def update_global_stylesheet(self, ratio):
        # 기준 폰트 크기 스케일링
        font_9 = int(9 * ratio)
        font_10 = int(10 * ratio)
        font_12 = int(12 * ratio)
        font_14 = int(14 * ratio)
        font_16 = int(16 * ratio)
        
        # 패딩 및 마진 스케일링
        padding_6 = int(6 * ratio)
        padding_8 = int(8 * ratio)
        padding_10 = int(10 * ratio)
        padding_15 = int(15 * ratio)
        padding_18 = int(18 * ratio)
        
        qss = f"""
            QMainWindow {{
                background-color: #0f172a;
            }}
            QTabWidget::pane {{
                border: 1px solid #1e293b;
                background-color: #0f172a;
                border-radius: {int(16*ratio)}px;
                padding: {padding_10}px;
            }}
            QTabBar::tab {{
                background-color: #1e293b;
                color: #94a3b8;
                border: 1px solid #334155;
                border-top-left-radius: {int(8*ratio)}px;
                border-top-right-radius: {int(8*ratio)}px;
                padding: {padding_10}px {padding_18}px;
                margin-right: {int(4*ratio)}px;
                font-size: {font_10}pt;
                font-weight: bold;
            }}
            QTabBar::tab:selected {{
                background-color: #2563eb;
                color: white;
                border-color: #2563eb;
            }}
            QGroupBox {{
                border: 1px solid #1e293b;
                border-radius: {int(12*ratio)}px;
                margin-top: {padding_15}px;
                padding-top: {padding_15}px;
                font-weight: bold;
                font-size: {font_10}pt;
                color: #38bdf8;
            }}
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {{
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: {int(8*ratio)}px;
                padding: {padding_8}px;
                color: white;
            }}
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
                border: 1px solid #2563eb;
            }}
            QPushButton {{
                background-color: #2563eb;
                color: white;
                border: none;
                border-radius: {int(10*ratio)}px;
                padding: {padding_10}px;
                font-size: {font_10}pt;
                font-weight: bold;
            }}
            QPushButton:hover {{
                background-color: #1d4ed8;
            }}
            QPushButton:disabled {{
                background-color: #334155;
                color: #64748b;
            }}
            QLabel {{
                color: #cbd5e1;
                font-size: {font_10}pt;
            }}
            QProgressBar {{
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: {int(8*ratio)}px;
                text-align: center;
                color: white;
                font-weight: bold;
                height: {int(20*ratio)}px;
            }}
            QProgressBar::chunk {{
                background-color: #10b981;
                border-radius: {int(6*ratio)}px;
            }}
            QListWidget {{
                background-color: #1e293b;
                border: 1px solid #334155;
                border-radius: {int(10*ratio)}px;
                color: white;
            }}
            QTextBrowser {{
                background-color: #020617;
                border: 1px solid #1e293b;
                border-radius: {int(10*ratio)}px;
                color: #38bdf8;
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: {font_9}pt;
            }}
            QRadioButton, QCheckBox {{
                color: #cbd5e1;
                font-size: {font_10}pt;
            }}
        """
        app = QtWidgets.QApplication.instance()
        if app:
            font = QtGui.QFont("Malgun Gothic", font_9)
            app.setFont(font)
            app.setStyleSheet(qss)

    def change_zoom_scale(self, ratio):
        self.update_global_stylesheet(ratio)
        try:
            config_path = APP_DIR / "config.yaml"
            if config_path.exists():
                import yaml
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
                if "ui" not in cfg:
                    cfg["ui"] = {}
                cfg["ui"]["zoom"] = ratio
                with open(config_path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(cfg, f, default_flow_style=False, allow_unicode=True)
                self.log_message(f"[설정] 화면 배율을 {int(ratio*100)}%로 저장했습니다.")
        except Exception as e:
            print(f"화면 배율 저장 실패: {e}")

    def save_credentials(self):
        import json
        import base64
        data = {
            "id": self.input_login_id.text().strip(),
            "pw": self.input_login_pw.text().strip(),
            "birth": self.input_login_birth.text().strip()
        }
        try:
            json_str = json.dumps(data, ensure_ascii=False)
            encoded = base64.b64encode(json_str.encode("utf-8")).decode("utf-8")
            (APP_DIR / ".credentials").write_text(encoded, encoding="utf-8")
        except Exception:
            pass

    def load_credentials(self):
        import json
        import base64
        cred_file = APP_DIR / ".credentials"
        if cred_file.exists():
            try:
                encoded = cred_file.read_text(encoding="utf-8").strip()
                decoded = base64.b64decode(encoded.encode("utf-8")).decode("utf-8")
                data = json.loads(decoded)
                self.input_login_id.setText(data.get("id", ""))
                self.input_login_pw.setText(data.get("pw", ""))
                self.input_login_birth.setText(data.get("birth", ""))
                self.check_save_credentials.setChecked(True)
            except Exception:
                pass

    def clear_credentials(self):
        try:
            cred_file = APP_DIR / ".credentials"
            if cred_file.exists():
                cred_file.unlink()
        except Exception:
            pass

    # --- Automation Runner (Tab 2: Solting 1-3) ---
    def start_automation(self):
        xlsx_path = self.input_excel.text().strip()
        if not xlsx_path or not os.path.exists(xlsx_path):
            QtWidgets.QMessageBox.warning(self, "경고", "올바른 엑셀 파일을 선택하세요.")
            return

        if not self.check_solting.isChecked() and not self.check_insurance.isChecked():
            QtWidgets.QMessageBox.warning(self, "경고", "최소 하나 이상의 가동 단계를 선택해야 합니다.")
            return

        # 방법3: 동의서(보험사) 단계가 진행되면, 완료 후 이 엑셀의 전화번호를 고객DB에 자동 적재
        self._last_consent_xlsx = xlsx_path
        self._last_consent_ran = self.check_insurance.isChecked()

        # 기존 config.yaml에서 전체 설정을 먼저 안전하게 로드합니다.
        try:
            from solting_auto.config import load_config
            config = load_config(str(APP_DIR / "config.yaml"))
        except Exception as e:
            config = {
                "stages": {},
                "run": {},
                "insurance": {
                    "browser": {},
                    "oz": {}
                },
                "columns": {},
                "format": {}
            }

        # 동적 사용자 입력 값 및 상태들을 덮어씁니다.
        config["stages"]["solting"] = self.check_solting.isChecked()
        config["stages"]["insurance"] = self.check_insurance.isChecked()
        
        config["run"]["output_folder"] = str(APP_DIR / "output")
        config["run"]["retry_count"] = 1
        config["run"]["retry_delay_sec"] = 2
        config["run"]["row_delay_sec"] = 1.0

        if "insurance" not in config:
            config["insurance"] = {}
        
        config["insurance"]["pdf_folder"] = self.input_pdf_folder.text().strip() or str(APP_DIR / "output" / "consent_pdfs")
        config["insurance"]["pdf_stamped_folder"] = self.input_pdf_stamped_folder.text().strip() or str(APP_DIR / "output" / "consent_pdfs_stamped")
        config["insurance"]["stamping_enabled"] = self.check_stamping.isChecked()
        config["insurance"]["kb_scan_enabled"] = self.check_kb_scan.isChecked()
        
        if "browser" not in config["insurance"]:
            config["insurance"]["browser"] = {}
        config["insurance"]["browser"]["mode"] = "attach"
        config["insurance"]["browser"]["cdp_url"] = "http://localhost:9222"
        config["insurance"]["browser"]["skip_login"] = True

        if "oz" not in config["insurance"]:
            config["insurance"]["oz"] = {}
        config["insurance"]["oz"]["file_format"] = self.combo_file_format.currentText()
        # PNG 저장 비율 옵션 (확대/축소 비율 %) — KB EDMS 스캔 인식률 향상용
        config["insurance"]["oz"]["save_ratio_enabled"] = self.check_save_ratio.isChecked()
        config["insurance"]["oz"]["save_ratio"] = self.spin_save_ratio.value()
        # 50명 단위 자동 폴더링 옵션 — KB 스캔 렉 완화용
        config["insurance"]["auto_folder_enabled"] = self.check_auto_folder.isChecked()
        config["insurance"]["auto_folder_interval"] = self.spin_folder_interval.value()
        # 기등록(로컬) 무시 재처리 옵션
        config["insurance"]["ignore_local_dedup"] = self.check_ignore_dedup.isChecked()
        config["insurance"]["input_mode"] = "batch" if self.radio_input_batch.isChecked() else "single"

        config["columns"] = {
            "jumin": "주민번호",
            "name": "성명",
            "phone": "휴대폰"
        }
        config["format"] = {
            "jumin_checksum": False
        }

        # Override delays/offsets from PyQt inputs
        config["insurance"]["offsets"] = {k: spin.value() for k, spin in self.offset_inputs.items()}
        config["insurance"]["delays"] = {k: spin.value() for k, spin in self.delay_inputs.items()}
        config["insurance"]["ratios"] = {
            "pop_send_x": 0.693989,
            "pop_send_y": 0.873817
        }

        # config.yaml에 최종 설정 보존 기록
        try:
            config_path = APP_DIR / "config.yaml"
            import yaml
            with open(config_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(config, f, default_flow_style=False, allow_unicode=True)
            self.log_message("[설정] config.yaml 파일에 실행 옵션을 보존했습니다.")
        except Exception as save_err:
            print(f"config.yaml 저장 중 오류: {save_err}")

        self.btn_run.setEnabled(False)
        self.btn_run.setText("구동 중...")
        self.btn_stop.setEnabled(True)
        self.console.clear()

        # Create execution log row via RPC
        try:
            log_res = self.supabase.rpc("create_execution_log_via_device", {
                "p_tenant_id": self.tenant["id"],
                "p_device_id": self.device["id"],
                "p_job_type": "excel_processing",
                "p_filename": Path(xlsx_path).name,
                "p_current_stage": "solting" if self.check_solting.isChecked() else "insurance"
            }).execute()
            log_id = log_res.data
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "오류", f"감사 로그 생성 실패: {e}")
            self.btn_run.setEnabled(True)
            self.btn_run.setText("▶️ 전산 처리 시작")
            self.btn_stop.setEnabled(False)
            return

        # Start worker thread
        self.current_job_worker = AutomationWorker(
            xlsx_path, 
            config, 
            self.check_dry_run.isChecked(), 
            self.signaler, 
            self.supabase, 
            log_id
        )
        import solting_auto.insurance
        solting_auto.insurance.active_instance = self.current_job_worker
        self.current_job_worker.start()

    def stop_automation_manually(self):
        if self.current_job_worker and self.current_job_worker.isRunning():
            self.btn_stop.setEnabled(False)
            self.btn_stop.setText("중단 처리 중...")
            self.current_job_worker.stop_requested = True
            
            try:
                self.supabase.rpc("update_execution_log_status_via_device", {
                    "p_log_id": self.current_job_worker.log_id,
                    "p_status": "stopped",
                    "p_error_reason": "사용자 로컬 중단 요청",
                    "p_error_screenshot_url": None,
                    "p_report_file_url": None
                }).execute()
            except Exception:
                pass

    def log_message(self, text):
        self.console.append(text)
        self.console.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def update_progress(self, done, total, last_msg):
        self.progress_bar.setMaximum(total)
        self.progress_bar.setValue(done)
        self.log_message(f"[진행률] {done}/{total} 완료 - {last_msg}")

    def automation_finished(self, success, msg, report_url):
        self.btn_run.setEnabled(True)
        self.btn_run.setText("▶️ 전산 처리 시작")
        self.btn_stop.setEnabled(False)
        self.btn_stop.setText("🛑 중단 (Ctrl+D)")
        
        if success:
            QtWidgets.QMessageBox.information(self, "완료", msg)
            if report_url:
                self.log_message(f"[완료] 결과 엑셀 리포트 URL: {report_url}")
            # 방법3: 동의서 단계 진행분의 전화번호를 고객DB에 자동 적재(매칭)
            if getattr(self, "_last_consent_ran", False):
                self._sync_phones_after_consent(getattr(self, "_last_consent_xlsx", None))
        else:
            QtWidgets.QMessageBox.critical(self, "작업 중단/오류", msg)

        import solting_auto.insurance
        solting_auto.insurance.active_instance = None
        self.current_job_worker = None

    def _sync_phones_after_consent(self, xlsx_path):
        """동의서 진행 엑셀에서 (이름/생년월일/전화)를 추출해 고객DB에 전화번호를 자동 적재.
        보장분석 수집 데이터와 (이름+생년월일)로 자동 병합된다(upsert COALESCE)."""
        try:
            if not (self.supabase and self.tenant and self.device):
                return
            if not xlsx_path or not os.path.exists(xlsx_path):
                return
            from solting_auto import kb_crawler
            contacts = kb_crawler._read_excel_contacts([xlsx_path])
            recs = [{"customer_name": c["customer_name"], "birth": c.get("birth", ""), "phone": c["phone"]}
                    for c in contacts if c.get("customer_name") and c.get("phone")]
            if not recs:
                return
            n = 0
            for i in range(0, len(recs), 100):
                chunk = recs[i:i + 100]
                res = self.supabase.rpc("upsert_customer_records_via_device", {
                    "p_tenant_id": self.tenant["id"],
                    "p_device_id": self.device["id"],
                    "p_records": chunk,
                }).execute()
                n += res.data if isinstance(res.data, int) else len(chunk)
            self.log_message(f"[고객DB] 동의서 엑셀 전화번호 {n}건을 고객DB에 자동 적재했습니다.")
        except Exception as e:
            self.log_message(f"[고객DB] 전화번호 자동 적재 생략(고객DB 미생성 등): {e}")

    # --- EDMS Automation (Tab 3) ---
    def start_edms_upload(self):
        if not self.edms_pdf_paths:
            QtWidgets.QMessageBox.warning(self, "경고", "업로드할 PDF 파일이 폴더에 없어요.")
            return

        # 기존 config.yaml에서 설정을 안전하게 로드합니다.
        try:
            from solting_auto.config import load_config
            config = load_config(str(APP_DIR / "config.yaml"))
        except Exception as e:
            config = {
                "run": {},
                "insurance": {}
            }

        config["run"]["output_folder"] = str(APP_DIR / "output")
        if "insurance" not in config:
            config["insurance"] = {}
        config["insurance"]["offsets"] = {k: spin.value() for k, spin in self.offset_inputs.items()}
        config["insurance"]["delays"] = {k: spin.value() for k, spin in self.delay_inputs.items()}
        config["insurance"]["ratios"] = {
            "pop_send_x": 0.693989,
            "pop_send_y": 0.873817
        }

        self.btn_edms_run.setEnabled(False)
        self.btn_edms_run.setText("업로드 중...")
        self.btn_edms_stop.setEnabled(True)
        self.edms_console.clear()

        # Create execution log row via RPC
        try:
            log_res = self.supabase.rpc("create_execution_log_via_device", {
                "p_tenant_id": self.tenant["id"],
                "p_device_id": self.device["id"],
                "p_job_type": "edms_upload",
                "p_filename": f"EDMS 일괄업로드 ({len(self.edms_pdf_paths)}개 파일)",
                "p_current_stage": "edms_upload"
            }).execute()
            log_id = log_res.data
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "오류", f"감사 로그 생성에 실패했어요:\n{e}")
            self.btn_edms_run.setEnabled(True)
            self.btn_edms_run.setText("▶️ EDMS 일괄 전송 시작")
            self.btn_edms_stop.setEnabled(False)
            return

        self.current_edms_worker = EDMSUploadWorker(
            self.edms_pdf_paths,
            config,
            self.signaler_edms,
            self.supabase,
            log_id
        )
        self.current_edms_worker.start()

    def stop_edms_upload_manually(self):
        if self.current_edms_worker and self.current_edms_worker.isRunning():
            self.btn_edms_stop.setEnabled(False)
            self.btn_edms_stop.setText("중단 처리 중...")
            self.current_edms_worker.stop_requested = True
            if hasattr(self.current_edms_worker, "active_auto") and self.current_edms_worker.active_auto:
                self.current_edms_worker.active_auto.stop_requested = True
            
            try:
                self.supabase.rpc("update_execution_log_status_via_device", {
                    "p_log_id": self.current_edms_worker.log_id,
                    "p_status": "stopped",
                    "p_error_reason": "사용자 로컬 중단 요청",
                    "p_error_screenshot_url": None,
                    "p_report_file_url": None
                }).execute()
            except Exception:
                pass

    def log_edms_message(self, text):
        self.edms_console.append(text)
        self.edms_console.moveCursor(QtGui.QTextCursor.MoveOperation.End)

    def update_edms_progress(self, done, total, last_msg):
        self.progress_bar_edms.setMaximum(total)
        self.progress_bar_edms.setValue(done)
        self.log_edms_message(f"[진행률] {done}/{total} 전송 완료 - {last_msg}")

    def edms_finished(self, success, msg, report_url):
        self.btn_edms_run.setEnabled(True)
        self.btn_edms_run.setText("▶️ EDMS 일괄 전송 시작")
        self.btn_edms_stop.setEnabled(False)
        self.btn_edms_stop.setText("🛑 중단 (Ctrl+D)")
        
        if success:
            QtWidgets.QMessageBox.information(self, "완료", msg)
        else:
            QtWidgets.QMessageBox.critical(self, "작업 중단/오류", msg)

        self.current_edms_worker = None

    # --- Global Ctrl+D Hotkey handler ---
    def handle_ctrl_d_shortcut(self):
        if self.current_job_worker and self.current_job_worker.isRunning():
            self.log_message("[알림] 단축키 Ctrl+D 감지 - 동의서 출력 작업을 중단합니다.")
            self.stop_automation_manually()
        elif self.current_edms_worker and self.current_edms_worker.isRunning():
            self.log_edms_message("[알림] 단축키 Ctrl+D 감지 - EDMS 업로드 작업을 중단합니다.")
            self.stop_edms_upload_manually()

    def closeEvent(self, event):
        if self.mouse_tracker.isRunning():
            self.mouse_tracker.stop()
        self.heartbeat_timer.stop()
        event.accept()


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    
    font = QtGui.QFont("Malgun Gothic", 9)
    app.setFont(font)
    app.setStyle("Fusion")

    # Premium dark style matching Toss UI guidelines
    dark_palette = QtGui.QPalette()
    dark_palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(15, 23, 42))
    dark_palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor(241, 245, 249))
    dark_palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor(30, 41, 59))
    dark_palette.setColor(QtGui.QPalette.ColorRole.AlternateBase, QtGui.QColor(15, 23, 42))
    dark_palette.setColor(QtGui.QPalette.ColorRole.ToolTipBase, QtGui.QColor(241, 245, 249))
    dark_palette.setColor(QtGui.QPalette.ColorRole.ToolTipText, QtGui.QColor(15, 23, 42))
    dark_palette.setColor(QtGui.QPalette.ColorRole.Text, QtGui.QColor(241, 245, 249))
    dark_palette.setColor(QtGui.QPalette.ColorRole.Button, QtGui.QColor(30, 41, 59))
    dark_palette.setColor(QtGui.QPalette.ColorRole.ButtonText, QtGui.QColor(241, 245, 249))
    dark_palette.setColor(QtGui.QPalette.ColorRole.BrightText, QtCore.Qt.GlobalColor.red)
    dark_palette.setColor(QtGui.QPalette.ColorRole.Link, QtGui.QColor(59, 130, 246))
    dark_palette.setColor(QtGui.QPalette.ColorRole.Highlight, QtGui.QColor(59, 130, 246))
    dark_palette.setColor(QtGui.QPalette.ColorRole.HighlightedText, QtGui.QColor(241, 245, 249))
    app.setPalette(dark_palette)

    # Stylesheet for sleek, premium borders and alignments
    app.setStyleSheet("""
        QMainWindow {
            background-color: #0f172a;
        }
        QTabWidget::pane {
            border: 1px solid #1e293b;
            background-color: #0f172a;
            border-radius: 16px;
            padding: 10px;
        }
        QTabBar::tab {
            background-color: #1e293b;
            color: #94a3b8;
            border: 1px solid #334155;
            border-top-left-radius: 8px;
            border-top-right-radius: 8px;
            padding: 10px 18px;
            margin-right: 4px;
            font-size: 10pt;
            font-weight: bold;
        }
        QTabBar::tab:selected {
            background-color: #2563eb;
            color: white;
            border-color: #2563eb;
        }
        QGroupBox {
            border: 1px solid #1e293b;
            border-radius: 12px;
            margin-top: 15px;
            padding-top: 15px;
            font-weight: bold;
            font-size: 10pt;
            color: #38bdf8;
        }
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
            background-color: #1e293b;
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 8px;
            color: white;
        }
        QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
            border: 1px solid #2563eb;
        }
        QPushButton {
            background-color: #2563eb;
            color: white;
            border: none;
            border-radius: 10px;
            padding: 10px;
            font-size: 10pt;
            font-weight: bold;
        }
        QPushButton:hover {
            background-color: #1d4ed8;
        }
        QPushButton:disabled {
            background-color: #334155;
            color: #64748b;
        }
        QLabel {
            color: #cbd5e1;
        }
        QProgressBar {
            background-color: #1e293b;
            border: 1px solid #334155;
            border-radius: 8px;
            text-align: center;
            color: white;
            font-weight: bold;
            height: 20px;
        }
        QProgressBar::chunk {
            background-color: #10b981;
            border-radius: 6px;
        }
        QListWidget {
            background-color: #1e293b;
            border: 1px solid #334155;
            border-radius: 10px;
            color: white;
        }
        QTextBrowser {
            background-color: #020617;
            border: 1px solid #1e293b;
            border-radius: 10px;
            color: #38bdf8;
            font-family: 'Consolas', 'Courier New', monospace;
            font-size: 9pt;
        }
    """)

    window = KkandoriAgent()
    window.show()
    sys.exit(app.exec())
