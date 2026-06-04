#!/usr/bin/env python3
"""솔팅프로그램 전산등록 자동화 - 웹 UI.

맥미니 등에서 서버를 실행하면 브라우저로 접속해 엑셀을 업로드하고
등록 진행 상황과 결과를 확인할 수 있다. 기존 solting_auto 모듈을 재사용한다.

실행:
  python web/app.py                 # localhost:8000
  HOST=0.0.0.0 PORT=8000 python web/app.py   # 사내망 공유
"""

import os
import sys
import threading
import uuid
from pathlib import Path

# 프로젝트 루트를 import 경로에 추가
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# exe(frozen) 대응:
#  - RES_DIR : 번들된 읽기전용 리소스(templates, config.example) 위치(_MEIPASS)
#  - APP_DIR : exe 옆의 쓰기 가능한 폴더(config.yaml, uploads, output)
FROZEN = getattr(sys, "frozen", False)
RES_DIR = Path(getattr(sys, "_MEIPASS", ROOT))
APP_DIR = Path(sys.executable).parent if FROZEN else ROOT

from flask import Flask, request, jsonify, send_file, render_template

from solting_auto.config import load_config
from solting_auto.logger import get_logger
from solting_auto.runner import process_file
from solting_auto.masking import mask_jumin, mask_phone



app = Flask(__name__, template_folder=str(RES_DIR / "web" / "templates"), static_folder=str(RES_DIR / "web" / "static"))

CONFIG_PATH = os.environ.get("SOLTING_CONFIG", str(APP_DIR / "config.yaml"))
UPLOAD_DIR = APP_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 메모리 작업 저장소 (PoC). 운영 시 영속 저장소로 교체 가능.
_jobs = {}
_lock = threading.Lock()
active_edms_auto = None


def _set(job_id, **kw):
    with _lock:
        _jobs[job_id].update(kw)


def _run_job(job_id, xlsx_path, dry_run, stages=None, pdf_folder=None, file_format=None, pdf_stamped_folder=None, stamping_enabled=True, kb_scan_enabled=True):
    try:
        cfg = load_config(CONFIG_PATH)
        if stages is not None:
            cfg["stages"] = stages
        if pdf_folder:
            cfg["insurance"]["pdf_folder"] = pdf_folder
        if pdf_stamped_folder:
            cfg["insurance"]["pdf_stamped_folder"] = pdf_stamped_folder
        cfg["insurance"]["stamping_enabled"] = stamping_enabled
        cfg["insurance"]["kb_scan_enabled"] = kb_scan_enabled
        if file_format:
            cfg["insurance"]["oz"]["file_format"] = file_format
        logger = get_logger(cfg["run"].get("output_folder", str(ROOT / "output")))
        
        logger.info(f"[웹 요청 수신] PDF 폴더 경로: {cfg['insurance'].get('pdf_folder')}")
        logger.info(f"[웹 요청 수신] 스탬프 폴더 경로: {cfg['insurance'].get('pdf_stamped_folder')}")
        logger.info(f"[웹 요청 수신] 스탬핑 활성화: {cfg['insurance'].get('stamping_enabled')}")
        logger.info(f"[웹 요청 수신] KB스캔 활성화: {cfg['insurance'].get('kb_scan_enabled')}")

        def progress(done, total, last):
            _set(job_id, done=done, total=total,
                 last=f"[{last.row_no}행] {last.status}"
                      + (f" - {last.reason}" if last.reason else ""))

        _set(job_id, state="running")
        summary = process_file(xlsx_path, cfg, logger, dry_run=dry_run, progress_cb=progress)

        rows = [{
            "row_no": r.row_no,
            "jumin": mask_jumin(r.jumin),
            "name": r.name,
            "phone": mask_phone(r.phone),
            "status": r.status,
            "reason": r.reason,
            "solting": r.solting_status,
            "solting_reason": r.solting_reason,
            "insurance": r.insurance_status,
            "insurance_reason": r.insurance_reason,
            "consent": bool(r.consent_pdf),
            "consent_stamped": bool(r.consent_stamped_pdf),
            "kb_scan": r.kb_scan_status,
            "kb_scan_reason": r.kb_scan_reason,
        } for r in summary.results]

        # 가장 최근 생성된 리포트 경로 찾기
        out_dir = Path(cfg["run"].get("output_folder", str(ROOT / "output")))
        reports = sorted(out_dir.glob("result_*.xlsx"), key=lambda p: p.stat().st_mtime)
        report_path = str(reports[-1].resolve()) if reports else ""

        _set(job_id, state="done", summary={
            "total": summary.total, "success": summary.success,
            "fail": summary.fail, "skip": summary.skip,
            "consent": summary.consent_count,
            "kb_scan": summary.kb_scan_count,
        }, rows=rows, report=report_path)
    except Exception as e:
        summary = getattr(e, "summary", None)
        if summary:
            try:
                rows = [{
                    "row_no": r.row_no,
                    "jumin": mask_jumin(r.jumin),
                    "name": r.name,
                    "phone": mask_phone(r.phone),
                    "status": r.status,
                    "reason": r.reason,
                    "solting": r.solting_status,
                    "solting_reason": r.solting_reason,
                    "insurance": r.insurance_status,
                    "insurance_reason": r.insurance_reason,
                    "consent": bool(r.consent_pdf),
                    "consent_stamped": bool(r.consent_stamped_pdf),
                    "kb_scan": r.kb_scan_status,
                    "kb_scan_reason": r.kb_scan_reason,
                } for r in summary.results]

                out_dir = Path(cfg["run"].get("output_folder", str(ROOT / "output")))
                reports = sorted(out_dir.glob("result_*.xlsx"), key=lambda p: p.stat().st_mtime)
                report_path = str(reports[-1].resolve()) if reports else ""

                _set(job_id, state="error", error=str(e), summary={
                    "total": summary.total, "success": summary.success,
                    "fail": summary.fail, "skip": summary.skip,
                    "consent": summary.consent_count,
                    "kb_scan": summary.kb_scan_count,
                }, rows=rows, report=report_path)
                return
            except Exception as inner_err:
                logger.error(f"오류 복구 및 임시결과 생성 중 예외 발생: {inner_err}")
                
        _set(job_id, state="error", error=str(e))


@app.route("/open-browser", methods=["POST"])
def open_browser():
    try:
        import subprocess
        # 1단계: start_edge_debug.bat 과 동일한 인자로 Edge 디버그 기동
        cmd = 'start "" msedge.exe --remote-debugging-port=9222 --user-data-dir="%USERPROFILE%\\kb-edge-debug" "https://nsales.kbinsure.co.kr/eus/ch/ch_index.jsp"'
        subprocess.Popen(cmd, shell=True)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/login-portal", methods=["POST"])
def login_portal():
    data = request.json or {}
    username = data.get("username")
    password = data.get("password")
    birthdate = data.get("birthdate")

    if not username or not password or not birthdate:
        return jsonify({"error": "아이디, 비밀번호, 생년월일을 모두 입력해 주세요."}), 400

    try:
        cfg = load_config(CONFIG_PATH)
        cfg["insurance"]["browser"]["mode"] = "attach"
        cfg["insurance"]["browser"]["cdp_url"] = "http://localhost:9222"
        cfg["insurance"]["browser"]["skip_login"] = False
        
        logger = get_logger(cfg["run"].get("output_folder", str(ROOT / "output")))
        
        from solting_auto.insurance import InsuranceAutomation
        with InsuranceAutomation(cfg, logger) as auto:
            auto.login(username=username, password=password, birthdate=birthdate, force=True)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    cfg = load_config(CONFIG_PATH)
    pdf_folder = cfg["insurance"].get("pdf_folder", "./output/consent_pdfs")
    pdf_stamped_folder = cfg["insurance"].get("pdf_stamped_folder", "./output/consent_pdfs_stamped")
    stamping_enabled = cfg["insurance"].get("stamping_enabled", True)
    kb_scan_enabled = cfg["insurance"].get("kb_scan_enabled", True)
    file_format = cfg["insurance"]["oz"].get("file_format", "PDF")
    try:
        pdf_folder_abs = str(Path(pdf_folder).resolve())
    except Exception:
        pdf_folder_abs = pdf_folder
        
    try:
        pdf_stamped_folder_abs = str(Path(pdf_stamped_folder).resolve())
    except Exception:
        pdf_stamped_folder_abs = pdf_stamped_folder
        
    return render_template(
        "index.html", 
        pdf_folder=pdf_folder_abs, 
        pdf_stamped_folder=pdf_stamped_folder_abs,
        stamping_enabled=stamping_enabled,
        kb_scan_enabled=kb_scan_enabled,
        file_format=file_format
    )


@app.route("/upload", methods=["POST"])
def upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "파일을 선택하세요."}), 400
    if not f.filename.lower().endswith((".xlsx", ".xls")):
        return jsonify({"error": "엑셀 파일(.xlsx)만 업로드 가능합니다."}), 400

    dry_run = request.form.get("dry_run") == "true"
    stages = {
        "solting": request.form.get("stage_solting", "true") == "true",
        "insurance": request.form.get("stage_insurance", "false") == "true",
    }
    pdf_folder = request.form.get("pdf_folder", "").strip()
    pdf_stamped_folder = request.form.get("pdf_stamped_folder", "").strip()
    stamping_enabled = request.form.get("stamping_enabled") == "true"
    kb_scan_enabled = request.form.get("kb_scan_enabled") == "true"
    file_format = request.form.get("file_format", "PDF").strip()

    job_id = uuid.uuid4().hex[:12]
    dest = UPLOAD_DIR / f"{job_id}_{f.filename}"
    f.save(dest)

    with _lock:
        _jobs[job_id] = {"state": "queued", "done": 0, "total": 0,
                         "last": "", "filename": f.filename}

    t = threading.Thread(
        target=_run_job, 
        args=(job_id, str(dest), dry_run, stages, pdf_folder, file_format, pdf_stamped_folder, stamping_enabled, kb_scan_enabled), 
        daemon=True
    )
    t.start()
    return jsonify({"job_id": job_id})


@app.route("/status/<job_id>")
def status(job_id):
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({"error": "작업을 찾을 수 없습니다."}), 404
    return jsonify(job)


@app.route("/download/<job_id>")
def download(job_id):
    with _lock:
        job = _jobs.get(job_id)
    if not job or not job.get("report"):
        return jsonify({"error": "리포트가 없습니다."}), 404
    return send_file(job["report"], as_attachment=True)


@app.route("/select-folder", methods=["POST"])
def select_folder():
    try:
        import sys
        import subprocess
        cmd = [
            sys.executable, "-c",
            "import tkinter as tk; from tkinter import filedialog; root=tk.Tk(); root.withdraw(); root.attributes('-topmost', True); print(filedialog.askdirectory(title='저장 폴더 선택'))"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
        selected_dir = res.stdout.strip()
        if selected_dir:
            return jsonify({"folder": os.path.normpath(selected_dir)})
        return jsonify({"folder": ""})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _run_edms_batch_upload(job_id, folder_path, files):
    global active_edms_auto
    try:
        cfg = load_config(CONFIG_PATH)
        logger = get_logger(cfg["run"].get("output_folder", str(ROOT / "output")))
        
        logger.info(f"[EDMS 일괄 업로드] 작업 개시: 폴더={folder_path}, 파일 {len(files)}개")
        _set(job_id, state="running")
        
        from solting_auto.insurance import InsuranceAutomation
        auto = InsuranceAutomation(cfg, logger)
        active_edms_auto = auto
        
        full_paths = [str(Path(folder_path) / f) for f in files]
        
        def progress(done, total, last):
            _set(job_id, done=done, total=total, last=last)
            
        success = auto.batch_upload_via_win32(full_paths, progress_cb=progress)
        
        if getattr(auto, "stop_requested", False):
            _set(job_id, state="error", error="사용자에 의해 매크로 중단됨", last="중단됨: 사용자에 의한 매크로 중단")
        elif success:
            _set(job_id, state="done", last="EDMS 일괄 전송 완료!")
        else:
            _set(job_id, state="error", error="EDMS 일괄 업로드 중 에러가 발생했습니다.")
            
    except Exception as e:
        _set(job_id, state="error", error=str(e))
    finally:
        active_edms_auto = None


@app.route("/edms/files", methods=["POST"])
def edms_files():
    try:
        data = request.json or {}
        folder_path = data.get("folder_path", "").strip()
        if not folder_path:
            return jsonify({"error": "폴더 경로가 필요합니다."}), 400
            
        path = Path(folder_path)
        if not path.exists() or not path.is_dir():
            return jsonify({"files": [], "warning": "폴더가 존재하지 않거나 올바른 경로가 아닙니다."})
            
        files = sorted([p.name for p in path.glob("*.pdf") if p.is_file()])
        return jsonify({"files": files})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/edms/batch-upload", methods=["POST"])
def edms_batch_upload():
    try:
        data = request.json or {}
        folder_path = data.get("folder_path", "").strip()
        files = data.get("files", [])
        if not folder_path or not files:
            return jsonify({"error": "폴더 경로와 전송할 파일 목록이 필요합니다."}), 400
            
        job_id = uuid.uuid4().hex[:12]
        with _lock:
            _jobs[job_id] = {
                "state": "queued",
                "done": 0,
                "total": len(files),
                "last": "일괄 업로드 대기 중...",
                "filename": "EDMS 일괄 전송"
            }
            
        t = threading.Thread(
            target=_run_edms_batch_upload,
            args=(job_id, folder_path, files),
            daemon=True
        )
        t.start()
        return jsonify({"job_id": job_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _calibrate_edms_coords():
    import pywinauto
    from pywinauto import findwindows
    import ctypes
    import win32gui
    import win32ui
    import win32con
    from PIL import Image
    import numpy as np
    from scipy import ndimage
    import time

    elements = findwindows.find_elements(title_re=".*EDMS.*")
    if not elements:
        raise ValueError("EDMS 팝업 창을 찾을 수 없습니다. EDMS 창이 열려 있고 화면에 표시되어 있어야 합니다.")
    
    edms_handle = elements[0].handle
    
    # 윈도우 활성화 및 포커싱 시도
    try:
        win32gui.ShowWindow(edms_handle, win32con.SW_MAXIMIZE)
        time.sleep(0.5)
        win32gui.SetForegroundWindow(edms_handle)
        time.sleep(0.5)
    except Exception as focus_err:
        pass

    rect = win32gui.GetWindowRect(edms_handle)
    win_x, win_y, win_x2, win_y2 = rect
    win_w = win_x2 - win_x
    win_h = win_y2 - win_y

    if win_w <= 0 or win_h <= 0:
        raise ValueError("EDMS 창 크기가 올바르지 않습니다.")

    # PrintWindow로 창 캡처
    hwndDC = win32gui.GetWindowDC(edms_handle)
    mfcDC = win32ui.CreateDCFromHandle(hwndDC)
    saveDC = mfcDC.CreateCompatibleDC()
    
    saveBitMap = win32ui.CreateBitmap()
    saveBitMap.CreateCompatibleBitmap(mfcDC, win_w, win_h)
    saveDC.SelectObject(saveBitMap)
    
    ctypes.windll.user32.PrintWindow(edms_handle, saveDC.GetSafeHdc(), 2)
    
    bmpinfo = saveBitMap.GetInfo()
    bmpstr = saveBitMap.GetBitmapBits(True)
    img = Image.frombuffer('RGB', (bmpinfo['bmWidth'], bmpinfo['bmHeight']), bmpstr, 'raw', 'BGRX', 0, 1)
    
    img_w, img_h = img.size
    
    scale_x = img_w / win_w
    scale_y = img_h / win_h
    
    # GDI 리소스 정리
    win32gui.DeleteObject(saveBitMap.GetHandle())
    saveDC.DeleteDC()
    mfcDC.DeleteDC()
    win32gui.ReleaseDC(edms_handle, hwndDC)
    
    arr = np.array(img)
    
    # 파란색 버튼 색상 범위 (RGB)
    blue_mask = (
        (arr[:,:,0] >= 30) & (arr[:,:,0] <= 140) &
        (arr[:,:,1] >= 60) & (arr[:,:,1] <= 180) &
        (arr[:,:,2] >= 160) & (arr[:,:,2] <= 255)
    )
    
    blue_labeled, blue_count = ndimage.label(blue_mask)
    blue_buttons = []
    for i in range(1, blue_count + 1):
        coords = np.where(blue_labeled == i)
        area = len(coords[0])
        if area < 100:
            continue
        y_min, y_max = coords[0].min(), coords[0].max()
        x_min, x_max = coords[1].min(), coords[1].max()
        w = x_max - x_min
        h = y_max - y_min
        cx = (x_min + x_max) / 2
        cy = (y_min + y_max) / 2
        
        if w < 20 or h < 8 or w > 500 or h > 100:
            continue
            
        blue_buttons.append({
            'cx_img': cx,
            'cy_img': cy,
            'cx_offset': int(cx / scale_x),
            'cy_offset': int(cy / scale_y),
        })
        
    blue_buttons.sort(key=lambda b: b['cx_img'])
    
    # 상단 30% 영역 내 파란색 버튼들
    top_threshold = img_h * 0.30
    top_blue = [b for b in blue_buttons if b['cy_img'] < top_threshold]
    
    detected = {}
    
    # 마지막 2개가 이미지추가, 전송
    if len(top_blue) >= 2:
        detected["image_add_x"] = top_blue[-2]["cx_offset"]
        detected["image_add_y"] = top_blue[-2]["cy_offset"]
        detected["send_x"] = top_blue[-1]["cx_offset"]
        detected["send_y"] = top_blue[-1]["cy_offset"]
        
    # 중간 영역 파란색 버튼들 중 전체선택 버튼 탐색 (x좌표가 비교적 작고 y좌표가 30% ~ 60% 범위)
    mid_blue = [b for b in blue_buttons if b['cy_img'] >= top_threshold and b['cy_img'] < img_h * 0.60]
    if mid_blue:
        mid_blue.sort(key=lambda b: b['cx_offset'])
        detected["select_all_x"] = mid_blue[0]["cx_offset"]
        detected["select_all_y"] = mid_blue[0]["cy_offset"]

    return detected


@app.route("/edms/config", methods=["GET", "POST"])
def edms_config_endpoint():
    config_file_path = APP_DIR / "edms_config.json"
    
    default_config = {
        "delays": {
            "dialog_open_wait": 1.0,
            "tab_click_wait": 1.0,
            "folder_expand_wait": 1.5,
            "search_wait": 2.0,
            "image_load_wait": 4.0,
            "select_all_wait": 1.0,
            "send_confirm_wait": 0.5,
            "success_alert_wait": 15.0
        },
        "offsets": {
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
            "pop_send_btn_x": 254,
            "pop_send_btn_y": 277,
            "fallback_pop_send_x": 923,
            "fallback_pop_send_y": 692
        },
        "ratios": {
            "pop_send_x": 0.693989,
            "pop_send_y": 0.873817
        }
    }
    
    if request.method == "GET":
        import json
        if config_file_path.exists():
            try:
                with open(config_file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return jsonify(data)
            except Exception as e:
                return jsonify({"error": f"설정 로딩 중 오류: {str(e)}"}), 500
        else:
            try:
                with open(config_file_path, "w", encoding="utf-8") as f:
                    json.dump(default_config, f, ensure_ascii=False, indent=2)
                return jsonify(default_config)
            except Exception as e:
                return jsonify({"error": f"기본 설정 파일 생성 실패: {str(e)}"}), 500
                
    elif request.method == "POST":
        import json
        try:
            submitted_data = request.json or {}
            
            clean_data = {
                "delays": {},
                "offsets": {},
                "ratios": {}
            }
            
            for k, default_val in default_config["delays"].items():
                val = submitted_data.get("delays", {}).get(k, default_val)
                clean_data["delays"][k] = float(val)
                
            for k, default_val in default_config["offsets"].items():
                val = submitted_data.get("offsets", {}).get(k, default_val)
                clean_data["offsets"][k] = int(val)
                
            for k, default_val in default_config["ratios"].items():
                val = submitted_data.get("ratios", {}).get(k, default_val)
                clean_data["ratios"][k] = float(val)
                
            with open(config_file_path, "w", encoding="utf-8") as f:
                json.dump(clean_data, f, ensure_ascii=False, indent=2)
                
            return jsonify({"success": True, "config": clean_data})
        except Exception as e:
            return jsonify({"error": f"설정 저장 실패: {str(e)}"}), 500


@app.route("/edms/calibrate", methods=["POST"])
def edms_calibrate_endpoint():
    try:
        detected = _calibrate_edms_coords()
        return jsonify({"success": True, "detected": detected})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 400


@app.route("/edms/mouse-position", methods=["GET"])
def edms_mouse_position_endpoint():
    try:
        import pyautogui
        import win32gui
        from pywinauto import findwindows
        
        mx, my = pyautogui.position()
        
        # 1) EDMS 메인 창 검색
        elements = findwindows.find_elements(title_re=".*EDMS.*")
        edms_found = False
        rx, ry = 0, 0
        win_x, win_y = 0, 0
        
        if elements:
            edms_hwnd = elements[0].handle
            rect = win32gui.GetWindowRect(edms_hwnd)
            win_x, win_y = rect[0], rect[1]
            rx = mx - win_x
            ry = my - win_y
            edms_found = True
            
        # 2) 이미지 추가 다이얼로그 또는 전송 확인 팝업 검색
        dialogs = findwindows.find_elements(title_re=".*이미지.*")
        if not dialogs:
            dialogs = findwindows.find_elements(title_re=".*전송.*")
        # 메인 창 hwnd는 제외
        if elements:
            dialogs = [d for d in dialogs if d.handle != elements[0].handle]
            
        popup_found = False
        px, py = 0, 0
        dx, dy = 0, 0
        
        if dialogs:
            dlg_hwnd = dialogs[0].handle
            dlg_rect = win32gui.GetWindowRect(dlg_hwnd)
            dx, dy = dlg_rect[0], dlg_rect[1]
            px = mx - dx
            py = my - dy
            popup_found = True
            
        return jsonify({
            "screen": {"x": int(mx), "y": int(my)},
            "edms": {"x": int(rx), "y": int(ry), "found": edms_found, "win_x": int(win_x), "win_y": int(win_y)},
            "popup": {"x": int(px), "y": int(py), "found": popup_found, "dx": int(dx), "dy": int(dy)}
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/edms/stop", methods=["POST"])
def edms_stop():
    global active_edms_auto
    if active_edms_auto:
        active_edms_auto.stop_requested = True
        return jsonify({"success": True, "message": "중단 요청이 전송되었습니다."})
    return jsonify({"success": False, "error": "현재 실행 중인 EDMS 매크로가 없습니다."}), 400


def listen_for_hotkey(stop_callback):
    import ctypes
    import ctypes.wintypes
    user32 = ctypes.windll.user32
    HOTKEY_ID = 1234
    MOD_CONTROL = 0x0002  # Ctrl
    VK_D = 0x44  # 'D'
    
    if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL, VK_D):
        print("[WARNING] Ctrl+D 단축키 등록 실패 (이미 사용 중일 수 있음)")
        return
        
    try:
        msg = ctypes.wintypes.MSG()
        while True:
            if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == 0x0312:  # WM_HOTKEY
                    if msg.wParam == HOTKEY_ID:
                        stop_callback()
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
    except Exception as e:
        print(f"[ERROR] 단축키 리스너 오류: {e}")
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID)


if __name__ == "__main__":
    def stop_callback():
        global active_edms_auto
        if active_edms_auto:
            active_edms_auto.stop_requested = True
            print("[HOTKEY] Ctrl+D 감지: EDMS 매크로 중단 요청 완료")
            
    import threading
    t = threading.Thread(target=listen_for_hotkey, args=(stop_callback,), daemon=True)
    t.start()

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"\n  솔팅 전산등록 웹 → http://{host if host != '0.0.0.0' else 'localhost'}:{port}\n")
    app.run(host=host, port=port, debug=False)
