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
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(dotenv_path=ROOT / ".env")

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
        # Start remote stop polling thread
        polling_thread = threading.Thread(target=self._poll_stop_status, daemon=True)
        polling_thread.start()

        # Build log-capturing custom logger
        logger = logging.getLogger("kkandori_agent")
        logger.setLevel(logging.INFO)
        handler = PyQtLogHandler(self.signaler)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(handler)

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
            # Update status in DB to running via RPC
            self.supabase.rpc("update_execution_log_status_via_device", {
                "p_log_id": self.log_id,
                "p_status": "running",
                "p_error_reason": None,
                "p_error_screenshot_url": None,
                "p_report_file_url": None
            }).execute()
            
            # Run Solting core automation suite
            summary = process_file(
                self.xlsx_path, 
                self.config, 
                logger, 
                dry_run=self.dry_run, 
                progress_cb=progress_cb
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
                    shot_path = ROOT / "output" / f"error_{self.log_id}.png"
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
        # Start remote stop polling thread
        polling_thread = threading.Thread(target=self._poll_stop_status, daemon=True)
        polling_thread.start()

        # Build log-capturing custom logger
        logger = logging.getLogger("kkandori_edms")
        logger.setLevel(logging.INFO)
        handler = PyQtLogHandler(self.signaler)
        handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(handler)

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


class KkandoriAgent(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("깐돌이 전산등록 자동화 에이전트")
        self.resize(900, 700)
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

        # Tab 2: Automation Main Control (Solting 1-3)
        self.tab_control = QtWidgets.QWidget()
        self.init_control_tab()
        self.tabs.addTab(self.tab_control, "▶️ 동의서 출력 (1~3단계)")
        self.tabs.setTabEnabled(1, False)

        # Tab 3: EDMS Batch Upload Tab
        self.tab_edms = QtWidgets.QWidget()
        self.init_edms_tab()
        self.tabs.addTab(self.tab_edms, "📁 EDMS 일괄 업로드")
        self.tabs.setTabEnabled(2, False)

        # Tab 4: Offsets Configuration
        self.tab_offsets = QtWidgets.QWidget()
        self.init_offsets_tab()
        self.tabs.addTab(self.tab_offsets, "🎯 세부 좌표 설정")
        self.tabs.setTabEnabled(3, False)

        # Tab 5: Delays Configuration
        self.tab_delays = QtWidgets.QWidget()
        self.init_delays_tab()
        self.tabs.addTab(self.tab_delays, "⏳ 딜레이 설정")
        self.tabs.setTabEnabled(4, False)

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

    def init_control_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_control)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)

        # File Select
        file_layout = QtWidgets.QHBoxLayout()
        self.input_excel = QtWidgets.QLineEdit()
        self.input_excel.setPlaceholderText("전산처리할 엑셀 파일을 선택하세요.")
        btn_browse = QtWidgets.QPushButton("📁 파일 선택")
        btn_browse.clicked.connect(self.browse_excel)
        file_layout.addWidget(self.input_excel)
        file_layout.addWidget(btn_browse)
        layout.addLayout(file_layout)

        # Settings checkboxes
        options_layout = QtWidgets.QHBoxLayout()
        self.check_solting = QtWidgets.QCheckBox("솔팅전산등록")
        self.check_solting.setChecked(True)
        self.check_insurance = QtWidgets.QCheckBox("보험사등록 (동의서)")
        self.check_insurance.setChecked(True)
        self.check_dry_run = QtWidgets.QCheckBox("검증만 수행 (Dry-Run)")
        
        options_layout.addWidget(self.check_solting)
        options_layout.addWidget(self.check_insurance)
        options_layout.addWidget(self.check_dry_run)
        layout.addLayout(options_layout)

        # Automation triggers
        trigger_layout = QtWidgets.QHBoxLayout()
        self.btn_run = QtWidgets.QPushButton("▶️ 전산 처리 시작")
        self.btn_run.setFont(QtGui.QFont("Inter", 11, QtGui.QFont.Weight.Bold))
        self.btn_run.setStyleSheet("background-color: #10b981; color: white; padding: 12px; border-radius: 6px;")
        self.btn_run.clicked.connect(self.start_automation)

        self.btn_stop = QtWidgets.QPushButton("🛑 중단 (Ctrl+D)")
        self.btn_stop.setFont(QtGui.QFont("Inter", 11, QtGui.QFont.Weight.Bold))
        self.btn_stop.setStyleSheet("background-color: #ef4444; color: white; padding: 12px; border-radius: 6px;")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.stop_automation_manually)

        trigger_layout.addWidget(self.btn_run)
        trigger_layout.addWidget(self.btn_stop)
        layout.addLayout(trigger_layout)

        # Progress bar
        self.progress_bar = QtWidgets.QProgressBar()
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)

        # Console Logs
        self.console = QtWidgets.QTextBrowser()
        layout.addWidget(self.console)

    def init_edms_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_edms)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)

        # Folder selection row
        folder_layout = QtWidgets.QHBoxLayout()
        self.input_edms_folder = QtWidgets.QLineEdit()
        self.input_edms_folder.setPlaceholderText("EDMS 일괄 업로드할 문서 폴더를 선택하세요.")
        btn_browse_folder = QtWidgets.QPushButton("📁 폴더 선택")
        btn_browse_folder.clicked.connect(self.browse_edms_folder)
        folder_layout.addWidget(self.input_edms_folder)
        folder_layout.addWidget(btn_browse_folder)
        layout.addLayout(folder_layout)

        # Found PDF files panel
        layout.addWidget(QtWidgets.QLabel("업로드 대상 파일 목록"))
        self.list_edms_files = QtWidgets.QListWidget()
        self.list_edms_files.setMaximumHeight(150)
        layout.addWidget(self.list_edms_files)

        # Control triggers
        trigger_layout = QtWidgets.QHBoxLayout()
        self.btn_edms_run = QtWidgets.QPushButton("▶️ EDMS 일괄 전송 시작")
        self.btn_edms_run.setFont(QtGui.QFont("Inter", 11, QtGui.QFont.Weight.Bold))
        self.btn_edms_run.setStyleSheet("background-color: #3b82f6; color: white; padding: 12px; border-radius: 6px;")
        self.btn_edms_run.clicked.connect(self.start_edms_upload)

        self.btn_edms_stop = QtWidgets.QPushButton("🛑 중단 (Ctrl+D)")
        self.btn_edms_stop.setFont(QtGui.QFont("Inter", 11, QtGui.QFont.Weight.Bold))
        self.btn_edms_stop.setStyleSheet("background-color: #ef4444; color: white; padding: 12px; border-radius: 6px;")
        self.btn_edms_stop.setEnabled(False)
        self.btn_edms_stop.clicked.connect(self.stop_edms_upload_manually)

        trigger_layout.addWidget(self.btn_edms_run)
        trigger_layout.addWidget(self.btn_edms_stop)
        layout.addLayout(trigger_layout)

        # Progress bar
        self.progress_bar_edms = QtWidgets.QProgressBar()
        self.progress_bar_edms.setValue(0)
        layout.addWidget(self.progress_bar_edms)

        # Console logs
        self.edms_console = QtWidgets.QTextBrowser()
        layout.addWidget(self.edms_console)

    def init_offsets_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_offsets)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)
        
        # Track mouse coordinates row
        track_layout = QtWidgets.QHBoxLayout()
        self.lbl_mouse_coords = QtWidgets.QLabel("마우스 위치 추적: 중지됨 (X: -, Y: -)")
        self.lbl_mouse_coords.setFont(QtGui.QFont("Consolas", 10))
        self.btn_track_mouse = QtWidgets.QPushButton("📡 마우스 좌표 추적 시작")
        self.btn_track_mouse.clicked.connect(self.toggle_mouse_tracking)
        track_layout.addWidget(self.lbl_mouse_coords)
        track_layout.addWidget(self.btn_track_mouse)
        layout.addLayout(track_layout)

        # Scroll Area for inputs
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        container = QtWidgets.QWidget()
        self.offsets_layout = QtWidgets.QFormLayout(container)

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
            self.offsets_layout.addRow(f"{korean_label}:", spin)

        scroll.setWidget(container)
        layout.addWidget(scroll)

        # Sync buttons
        sync_layout = QtWidgets.QHBoxLayout()
        btn_load = QtWidgets.QPushButton("⬇️ 서버에서 불러오기")
        btn_load.clicked.connect(self.load_config_from_server)
        self.btn_save_server = QtWidgets.QPushButton("⬆️ 서버에 업로드")
        self.btn_save_server.clicked.connect(self.save_config_to_server)

        sync_layout.addWidget(btn_load)
        sync_layout.addWidget(self.btn_save_server)
        layout.addLayout(sync_layout)

    def init_delays_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_delays)
        layout.setContentsMargins(15, 15, 15, 15)
        layout.setSpacing(12)
        
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        container = QtWidgets.QWidget()
        self.delays_layout = QtWidgets.QFormLayout(container)

        self.delay_inputs = {}
        default_keys = [
            "dialog_open_wait", "tab_click_wait", "folder_expand_wait", "search_wait",
            "image_load_wait", "select_all_wait", "send_confirm_wait", "success_alert_wait"
        ]
        for key in default_keys:
            spin = QtWidgets.QDoubleSpinBox()
            spin.setRange(0.0, 120.0)
            spin.setSingleStep(0.1)
            spin.setValue(1.0)
            self.delay_inputs[key] = spin
            korean_label = DELAY_LABELS.get(key, key)
            self.delays_layout.addRow(f"{korean_label}:", spin)

        scroll.setWidget(container)
        layout.addWidget(scroll)

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
        self.tabs.setTabEnabled(3, True)
        self.tabs.setTabEnabled(4, True)
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
        self.tabs.setTabEnabled(4, False)

    def load_local_pin(self):
        pin_file = ROOT / ".pin"
        if pin_file.exists():
            try:
                return pin_file.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        return None

    def save_local_pin(self, pin):
        try:
            (ROOT / ".pin").write_text(pin, encoding="utf-8")
        except Exception:
            pass

    def delete_local_pin(self):
        try:
            pin_file = ROOT / ".pin"
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

        try:
            self.supabase.rpc("save_macro_config_via_device", {
                "p_tenant_id": self.tenant["id"],
                "p_offsets": offsets,
                "p_delays": delays
            }).execute()
            QtWidgets.QMessageBox.information(self, "성공", "서버에 매크로 설정 저장 완료!")
            self.log_message("[설정] 매크로 설정 좌표/딜레이 정보를 서버에 업로드 완료했어요.")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "오류", f"설정 저장 실패: {e}")

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

    # --- File browsers ---
    def browse_excel(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "엑셀 파일 선택", "", "Excel Files (*.xlsx *.xls)")
        if file_path:
            self.input_excel.setText(file_path)

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
            pdfs = sorted(folder.glob("*.pdf"), key=lambda p: p.name)
            for p in pdfs:
                self.list_edms_files.addItem(f"📄 {p.name} ({p.stat().st_size // 1024} KB)")
                self.edms_pdf_paths.append(str(p.resolve()))
            self.log_edms_message(f"[스캔] {len(self.edms_pdf_paths)}개의 PDF 파일을 감지했어요.")

    # --- Automation Runner (Tab 2: Solting 1-3) ---
    def start_automation(self):
        xlsx_path = self.input_excel.text().strip()
        if not xlsx_path or not os.path.exists(xlsx_path):
            QtWidgets.QMessageBox.warning(self, "경고", "올바른 엑셀 파일을 선택하세요.")
            return

        if not self.check_solting.isChecked() and not self.check_insurance.isChecked():
            QtWidgets.QMessageBox.warning(self, "경고", "최소 하나 이상의 가동 단계를 선택해야 합니다.")
            return

        config = {
            "stages": {
                "solting": self.check_solting.isChecked(),
                "insurance": self.check_insurance.isChecked()
            },
            "run": {
                "output_folder": str(ROOT / "output"),
                "retry_count": 1,
                "retry_delay_sec": 2,
                "row_delay_sec": 1.0
            },
            "insurance": {
                "pdf_folder": str(ROOT / "output" / "consent_pdfs"),
                "pdf_stamped_folder": str(ROOT / "output" / "consent_pdfs_stamped"),
                "stamping_enabled": True,
                "kb_scan_enabled": True,
                "browser": {
                    "mode": "attach",
                    "cdp_url": "http://localhost:9222",
                    "skip_login": True
                },
                "oz": {
                    "file_format": "PDF"
                }
            },
            "columns": {
                "jumin": "주민번호",
                "name": "성명",
                "phone": "휴대폰"
            },
            "format": {
                "jumin_checksum": False
            }
        }

        # Override delays/offsets from PyQt inputs
        config["insurance"]["offsets"] = {k: spin.value() for k, spin in self.offset_inputs.items()}
        config["insurance"]["delays"] = {k: spin.value() for k, spin in self.delay_inputs.items()}
        config["insurance"]["ratios"] = {
            "pop_send_x": 0.693989,
            "pop_send_y": 0.873817
        }

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
        else:
            QtWidgets.QMessageBox.critical(self, "작업 중단/오류", msg)

        import solting_auto.insurance
        solting_auto.insurance.active_instance = None
        self.current_job_worker = None

    # --- EDMS Automation (Tab 3) ---
    def start_edms_upload(self):
        if not self.edms_pdf_paths:
            QtWidgets.QMessageBox.warning(self, "경고", "업로드할 PDF 파일이 폴더에 없어요.")
            return

        config = {
            "run": {
                "output_folder": str(ROOT / "output"),
            },
            "insurance": {
                "offsets": {k: spin.value() for k, spin in self.offset_inputs.items()},
                "delays": {k: spin.value() for k, spin in self.delay_inputs.items()},
                "ratios": {
                    "pop_send_x": 0.693989,
                    "pop_send_y": 0.873817
                }
            }
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
