import os
import sys
import time
import uuid
import hashlib
import logging
import threading
import subprocess
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
            # Update DB progress in real-time
            try:
                self.supabase.table("execution_logs").update({
                    "progress_done": done,
                    "progress_total": total,
                    "last_message": f"[{last_result.row_no}행] {last_result.status}"
                }).eq("id", self.log_id).execute()
            except Exception:
                pass

        try:
            # Update status in DB to running
            self.supabase.table("execution_logs").update({"status": "running"}).eq("id", self.log_id).execute()
            
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
                self.supabase.table("execution_logs").update({
                    "status": "stopped",
                    "ended_at": "now()"
                }).eq("id", self.log_id).execute()
                self.signaler.finished_signal.emit(False, "사용자 중단 요청으로 종료되었습니다.", "")
                return

            # Find latest output report
            out_dir = Path(self.config["run"].get("output_folder", "./output"))
            reports = sorted(out_dir.glob("result_*.xlsx"), key=lambda p: p.stat().st_mtime)
            report_path = str(reports[-1].resolve()) if reports else ""

            # Upload report to Supabase Storage if found (Optional, but useful)
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

            # Update status in DB to success
            self.supabase.table("execution_logs").update({
                "status": "success",
                "report_file_url": report_url,
                "ended_at": "now()"
            }).eq("id", self.log_id).execute()

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

            # Update DB with error status
            try:
                self.supabase.table("execution_logs").update({
                    "status": "stopped" if is_stopped else "failed",
                    "error_reason": str(e),
                    "error_screenshot_url": screenshot_url,
                    "ended_at": "now()"
                }).eq("id", self.log_id).execute()
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
                res = self.supabase.table("execution_logs").select("status").eq("id", self.log_id).execute()
                if res.data and res.data[0]["status"] == "stopped":
                    print("[POLLING] 원격 중단 신호 감지!")
                    self.stop_requested = True
                    # Attempt to propagate stop flag to automation instance if it exists
                    # We inject a stop_requested property on modules/objects
                    from solting_auto.insurance import InsuranceAutomation
                    # Set the flag system-wide
                    # Our checks in solting_auto/insurance.py check for active_instance stop_requested
                    # We will set a class-level or global flag as safety
                    import solting_auto.insurance
                    # Set on all running instances
                    self.stop_requested = True
            except Exception:
                pass
            time.sleep(2)


class KkandoriAgent(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("깐돌이 전산등록 자동화 에이전트")
        self.resize(800, 650)
        self.supabase = None
        self.session = None
        self.profile = None
        self.tenant = None
        self.hwid = get_hwid()
        self.device = None
        self.current_job_worker = None
        
        self.signaler = Signaler()
        self.signaler.log_signal.connect(self.log_message)
        self.signaler.progress_signal.connect(self.update_progress)
        self.signaler.finished_signal.connect(self.automation_finished)

        self.mouse_tracker = MouseTracker()
        self.mouse_tracker.position_signal.connect(self.update_mouse_pos)

        self.init_ui()
        self.connect_supabase()

    def init_ui(self):
        # Central Tab Widget
        self.tabs = QtWidgets.QTabWidget()
        self.setCentralWidget(self.tabs)

        # Tab 1: Login & Status
        self.tab_auth = QtWidgets.QWidget()
        self.init_auth_tab()
        self.tabs.addTab(self.tab_auth, "🔐 계정 로그인")

        # Tab 2: Automation Main Control
        self.tab_control = QtWidgets.QWidget()
        self.init_control_tab()
        self.tabs.addTab(self.tab_control, "▶️ 전산 등록 실행")
        self.tabs.setTabEnabled(1, False)

        # Tab 3: Offsets Configuration
        self.tab_offsets = QtWidgets.QWidget()
        self.init_offsets_tab()
        self.tabs.addTab(self.tab_offsets, "🎯 세부 좌표 설정")
        self.tabs.setTabEnabled(2, False)

        # Tab 4: Delays Configuration
        self.tab_delays = QtWidgets.QWidget()
        self.init_delays_tab()
        self.tabs.addTab(self.tab_delays, "⏳ 딜레이 설정")
        self.tabs.setTabEnabled(3, False)

        # Status Bar
        self.statusBar().showMessage(f"기기 식별자(HWID): {self.hwid}")

    def init_auth_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_auth)
        layout.setContentsMargins(50, 50, 50, 50)
        layout.setSpacing(15)

        title = QtWidgets.QLabel("깐돌이 B2B SaaS 에이전트")
        title.setFont(QtGui.QFont("Inter", 18, QtGui.QFont.Weight.Bold))
        title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        subtitle = QtWidgets.QLabel("로그인하여 서버와 설정을 동기화하세요.")
        subtitle.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(subtitle)

        form_group = QtWidgets.QGroupBox("계정 정보")
        form_layout = QtWidgets.QFormLayout(form_group)
        
        self.input_email = QtWidgets.QLineEdit()
        self.input_email.setPlaceholderText("admin@company.com")
        self.input_password = QtWidgets.QLineEdit()
        self.input_password.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.input_password.setPlaceholderText("비밀번호 입력")
        
        form_layout.addRow("이메일:", self.input_email)
        form_layout.addRow("비밀번호:", self.input_password)
        layout.addWidget(form_group)

        self.btn_login = QtWidgets.QPushButton("🔐 로그인")
        self.btn_login.setFont(QtGui.QFont("Inter", 11, QtGui.QFont.Weight.Bold))
        self.btn_login.setStyleSheet("background-color: #3b82f6; color: white; padding: 10px; border-radius: 6px;")
        self.btn_login.clicked.connect(self.login_user)
        layout.addWidget(self.btn_login)

        # Status Panel
        self.info_box = QtWidgets.QGroupBox("기기 및 로그인 정보")
        self.info_layout = QtWidgets.QTextBrowser()
        self.info_layout.setPlaceholderText("로그인 상태 대기 중...")
        box_layout = QtWidgets.QVBoxLayout(self.info_box)
        box_layout.addWidget(self.info_layout)
        layout.addWidget(self.info_box)

    def init_control_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_control)

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
        self.console.setStyleSheet("background-color: #1e293b; color: #38bdf8; font-family: Consolas; font-size: 11px;")
        layout.addWidget(self.console)

    def init_offsets_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_offsets)
        
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
        # We will populate these keys dynamically from macro config
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
            self.offsets_layout.addRow(f"{key}:", spin)

        scroll.setWidget(container)
        layout.addWidget(scroll)

        # Sync buttons
        sync_layout = QtWidgets.QHBoxLayout()
        btn_load = QtWidgets.QPushButton("⬇️ 서버에서 불러오기")
        btn_load.clicked.connect(self.load_config_from_server)
        self.btn_save_server = QtWidgets.QPushButton("⬆️ 서버에 업로드 (관리자전용)")
        self.btn_save_server.clicked.connect(self.save_config_to_server)
        self.btn_save_server.setEnabled(False)

        sync_layout.addWidget(btn_load)
        sync_layout.addWidget(self.btn_save_server)
        layout.addLayout(sync_layout)

    def init_delays_tab(self):
        layout = QtWidgets.QVBoxLayout(self.tab_delays)
        
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
            self.delays_layout.addRow(f"{key} (초):", spin)

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

    def login_user(self):
        email = self.input_email.text().strip()
        password = self.input_password.text()

        if not email or not password:
            QtWidgets.QMessageBox.warning(self, "경고", "이메일과 비밀번호를 입력해 주세요.")
            return

        self.btn_login.setEnabled(False)
        self.btn_login.setText("로그인 중...")

        try:
            res = self.supabase.auth.sign_in_with_password({"email": email, "password": password})
            self.session = res.session
            
            # Fetch user profile
            profile_res = self.supabase.table("profiles").select("*").eq("id", self.session.user.id).execute()
            if not profile_res.data:
                raise ValueError("프로필을 찾을 수 없습니다.")
            self.profile = profile_res.data[0]

            tenant_id = self.profile.get("tenant_id")
            if not tenant_id:
                # User has not been linked to a tenant yet
                raise ValueError("소속 회사(Tenant)가 지정되지 않았습니다. 관리자에게 문의하세요.")

            # Fetch tenant name
            tenant_res = self.supabase.table("tenants").select("*").eq("id", tenant_id).execute()
            if not tenant_res.data:
                raise ValueError("회사를 찾을 수 없습니다.")
            self.tenant = tenant_res.data[0]

            # Register/retrieve device HWID
            self.register_device(tenant_id)

        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "오류", f"로그인 실패: {e}")
            self.btn_login.setEnabled(True)
            self.btn_login.setText("🔐 로그인")
            return

    def register_device(self, tenant_id):
        try:
            # Query existing device by HWID
            res = self.supabase.table("devices").select("*").eq("hwid", self.hwid).execute()
            if res.data:
                self.device = res.data[0]
                if self.device["status"] == "blocked":
                    raise ValueError("차단된 기기입니다. 관리자에게 문의하세요.")
            else:
                # Register new device
                reg_res = self.supabase.table("devices").insert({
                    "tenant_id": tenant_id,
                    "hwid": self.hwid,
                    "device_name": platform.node(),
                    "status": "pending"
                }).execute()
                self.device = reg_res.data[0]
                QtWidgets.QMessageBox.information(self, "안내", "신규 기기 등록 요청이 송신되었습니다. 관리자 승인 후 구동 가능합니다.")

            # Update heartbeat
            self.supabase.table("devices").update({"last_heartbeat": "now()"}).eq("id", self.device["id"]).execute()

            # Enable tabs
            if self.device["status"] == "approved":
                self.tabs.setTabEnabled(1, True)
                self.tabs.setTabEnabled(2, True)
                self.tabs.setTabEnabled(3, True)
                self.tabs.setCurrentIndex(1) # Switch to automation control
                
                # Check user role for config editing
                if self.profile["role"] in ["super_admin", "tenant_admin"]:
                    self.btn_save_server.setEnabled(True)
            
            # Show login info
            info_text = (
                f"접속자: {self.profile['name']} ({self.profile['role']})\n"
                f"소속 회사: {self.tenant['name']}\n"
                f"기기명: {self.device['device_name']}\n"
                f"기기 상태: {self.device['status']}\n"
                f"HWID: {self.hwid}"
            )
            self.info_layout.setText(info_text)
            self.btn_login.setText("✅ 로그인 완료")

            # Load configuration automatically
            self.load_config_from_server()

        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "기기 인증 오류", str(e))
            self.btn_login.setEnabled(True)
            self.btn_login.setText("🔐 로그인")

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

    def browse_excel(self):
        file_path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "엑셀 파일 선택", "", "Excel Files (*.xlsx *.xls)")
        if file_path:
            self.input_excel.setText(file_path)

    def load_config_from_server(self):
        if not self.tenant:
            return
        try:
            res = self.supabase.table("macro_configs").select("*").eq("tenant_id", self.tenant["id"]).execute()
            if res.data:
                config = res.data[0]
                offsets = config.get("offsets", {})
                delays = config.get("delays", {})

                for k, spin in self.offset_inputs.items():
                    if k in offsets:
                        spin.setValue(int(offsets[k]))

                for k, spin in self.delay_inputs.items():
                    if k in delays:
                        spin.setValue(float(delays[k]))

                self.log_message("[설정] 서버에서 최신 좌표 및 딜레이 수신 완료.")
            else:
                self.log_message("[경고] 서버에 저장된 좌표 설정이 없습니다. 기본값을 로드합니다.")
        except Exception as e:
            self.log_message(f"[에러] 서버 설정 로딩 실패: {e}")

    def save_config_to_server(self):
        if not self.tenant:
            return
        
        offsets = {k: spin.value() for k, spin in self.offset_inputs.items()}
        delays = {k: spin.value() for k, spin in self.delay_inputs.items()}
        ratios = {
            "pop_send_x": 0.693989,
            "pop_send_y": 0.873817
        }

        try:
            self.supabase.table("macro_configs").upsert({
                "tenant_id": self.tenant["id"],
                "offsets": offsets,
                "delays": delays,
                "ratios": ratios,
                "updated_at": "now()",
                "updated_by": self.profile["id"]
            }).execute()
            QtWidgets.QMessageBox.information(self, "성공", "서버에 매크로 설정 저장 완료!")
            self.log_message("[설정] 관리자 권한으로 설정 저장 완료.")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "오류", f"설정 저장 실패: {e}")

    def start_automation(self):
        xlsx_path = self.input_excel.text().strip()
        if not xlsx_path or not os.path.exists(xlsx_path):
            QtWidgets.QMessageBox.warning(self, "경고", "올바른 엑셀 파일을 선택하세요.")
            return

        if not self.check_solting.isChecked() and not self.check_insurance.isChecked():
            QtWidgets.QMessageBox.warning(self, "경고", "최소 하나 이상의 가동 단계를 선택해야 합니다.")
            return

        # Prepare configuration object structure matching runner's schema
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

        # Create execution log row in database
        try:
            log_res = self.supabase.table("execution_logs").insert({
                "tenant_id": self.tenant["id"],
                "user_id": self.profile["id"],
                "device_id": self.device["id"],
                "job_type": "excel_processing",
                "filename": Path(xlsx_path).name,
                "status": "queued",
                "progress_done": 0,
                "progress_total": 0,
                "current_stage": "solting" if self.check_solting.isChecked() else "insurance"
            }).execute()
            log_id = log_res.data[0]["id"]
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "오류", f"감사 로그 생성 실패: {e}")
            self.btn_run.setEnabled(True)
            self.btn_run.setText("▶️ 전산 처리 시작")
            self.btn_stop.setEnabled(False)
            return

        # Start automation worker thread
        self.current_job_worker = AutomationWorker(
            xlsx_path, 
            config, 
            self.check_dry_run.isChecked(), 
            self.signaler, 
            self.supabase, 
            log_id
        )
        # Link stop request system-wide in active module
        import solting_auto.insurance
        solting_auto.insurance.active_instance = self.current_job_worker
        
        self.current_job_worker.start()

    def stop_automation_manually(self):
        if self.current_job_worker and self.current_job_worker.isRunning():
            self.btn_stop.setEnabled(False)
            self.btn_stop.setText("중단 처리 중...")
            self.current_job_worker.stop_requested = True
            
            # Update DB log status to stopped
            try:
                self.supabase.table("execution_logs").update({
                    "status": "stopped",
                    "ended_at": "now()"
                }).eq("id", self.current_job_worker.log_id).execute()
            except Exception:
                pass

    def log_message(self, text):
        self.console.append(text)
        # Scroll console to bottom
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

        # Clear global reference
        import solting_auto.insurance
        solting_auto.insurance.active_instance = None
        self.current_job_worker = None

    def closeEvent(self, event):
        # Stop mouse tracker thread if running
        if self.mouse_tracker.isRunning():
            self.mouse_tracker.stop()
        event.accept()


# Register custom RegisterHotKey handler for Ctrl+D inside PyQt MainWindow
# We override native winEvent to catch WM_HOTKEY
# Since QMainWindow has nativeEvent on Windows:
class KkandoriAgentWin(KkandoriAgent):
    def __init__(self):
        super().__init__()
        self.setup_hotkey()

    def setup_hotkey(self):
        # Register Ctrl+D hotkey via ctypes
        import ctypes
        user32 = ctypes.windll.user32
        self.HOTKEY_ID = 1001
        MOD_CONTROL = 0x0002
        VK_D = 0x44
        # self.winId() returns HWND
        hwnd = int(self.winId())
        if not user32.RegisterHotKey(hwnd, self.HOTKEY_ID, MOD_CONTROL, VK_D):
            self.log_message("[경고] 윈도우 Ctrl+D 중단 단축키 등록 실패 (이미 사용 중일 수 있음)")

    def nativeEvent(self, eventType, message):
        import ctypes.wintypes
        msg = ctypes.wintypes.MSG.from_address(int(message))
        if msg.message == 0x0312: # WM_HOTKEY
            if msg.wParam == self.HOTKEY_ID:
                self.log_message("[단축키] Ctrl+D 감지: 매크로 원격/로컬 중단 처리를 트리거합니다.")
                self.stop_automation_manually()
        return super().nativeEvent(eventType, message)


if __name__ == "__main__":
    app = QtWidgets.QApplication(sys.argv)
    # Set premium dark style or Outfit font
    font = QtGui.QFont("Inter", 9)
    app.setFont(font)
    
    # Premium Dark Fusion style
    app.setStyle("Fusion")
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

    window = KkandoriAgentWin()
    window.show()
    sys.exit(app.exec())
