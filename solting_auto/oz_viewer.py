"""OZ 리포트 뷰어(OZ Report Viewer) → PDF 저장. (Windows 전용)

KB '동의서 출력'은 OZ 리포트 뷰어라는 데스크톱 앱으로 열린다. 브라우저 다운로드가
아니므로, Windows UI 자동화(pywinauto)로 'Microsoft Print to PDF' 프린터를 통해
PDF로 저장한다.

흐름(method="print_to_pdf"):
  OZ 뷰어 창 찾기 → Ctrl+P(인쇄) → 'Microsoft Print to PDF' 선택 → 인쇄
   → '프린터 출력을 다른 이름으로 저장' 대화상자에 경로 입력 → 저장

pywinauto 는 Windows 에서만 동작하며, 이 모듈은 함수 내부에서 지연 import 하므로
맥/리눅스의 dry-run 에는 영향을 주지 않는다.

※ 실제 Windows 환경의 OS 버전/언어/OZ 버전에 따라 대화상자 명칭이 달라질 수 있어,
  최초 1회 tools/test_oz_pdf.py 로 점검하며 config 의 명칭/타임아웃을 조정해야 한다.
"""

import time
from pathlib import Path


# 인쇄/저장 대화상자 제목 후보(한/영, OS 버전별)
_PRINT_DIALOG_TITLES = ["인쇄", "Print"]
_SAVE_DIALOG_TITLES = [
    "프린터 출력을 다른 이름으로 저장", "다른 이름으로 인쇄 출력 저장",
    "Save Print Output As", "다른 이름으로 저장", "Save As",
]


class OzError(Exception):
    pass


def save_as_pdf(dest_path: str, oz_cfg: dict, logger) -> str:
    """OZ 뷰어에 떠 있는 보고서를 dest_path 로 PDF 저장. 성공 시 경로 반환."""
    method = oz_cfg.get("method", "print_to_pdf")
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    from pywinauto import Desktop
    from pywinauto.keyboard import send_keys

    oz_win = _find_oz_window(oz_cfg, logger)
    oz_win.set_focus()
    time.sleep(0.5)

    try:
        if method == "save_as":
            res_path = _save_via_viewer(oz_win, dest, oz_cfg, logger, send_keys, Desktop)
        else:
            res_path = _print_to_pdf(oz_win, dest, oz_cfg, logger, send_keys, Desktop)
        return res_path
    finally:
        _close_oz_window(oz_win, logger, send_keys)


def _close_oz_window(oz_win, logger, send_keys):
    try:
        logger.info("OZ 리포트 뷰어 창 닫기 시도")
        oz_win.close()
        time.sleep(1.0)
    except Exception as close_err:
        logger.warning(f"oz_win.close() 에러: {close_err}")

    # win32gui를 사용한 보다 정확한 창 존재 여부 판별
    still_exists = False
    try:
        import win32gui
        still_exists = win32gui.IsWindow(oz_win.handle)
    except Exception:
        try:
            oz_win.window_text()
            still_exists = True
        except Exception:
            still_exists = False

    if still_exists:
        logger.info("OZ 뷰어가 여전히 존재하여 강제 종료 시도")
        try:
            import win32gui
            import win32con
            win32gui.PostMessage(oz_win.handle, win32con.WM_CLOSE, 0, 0)
            time.sleep(0.5)
        except Exception as win32_err:
            logger.debug(f"win32 PostMessage WM_CLOSE 실패: {win32_err}")

        try:
            oz_win.set_focus()
            send_keys("%{F4}")
            time.sleep(0.5)
        except Exception as f4_err:
            logger.debug(f"Alt+F4 전송 중 오류: {f4_err}")

    # 최종 안전장치: ozcviewer.exe 프로세스 강제 종료
    try:
        import subprocess
        subprocess.run(["taskkill", "/f", "/im", "ozcviewer.exe"], capture_output=True)
        logger.info("OZ 뷰어 프로세스(ozcviewer.exe) 강제 종료 완료")
    except Exception as kill_err:
        logger.debug(f"OZ 프로세스 taskkill 실패: {kill_err}")




def _find_oz_window(oz_cfg, logger):
    from pywinauto import Desktop

    keywords = oz_cfg.get("window_title_keywords", ["오즈 리포트 뷰어", "OZ Report"])
    timeout = oz_cfg.get("open_timeout_sec", 20)
    deadline = time.time() + timeout
    desktop = Desktop(backend="uia")
    while time.time() < deadline:
        for w in desktop.windows():
            try:
                title = w.window_text() or ""
            except Exception:
                continue
            if any(k in title for k in keywords):
                logger.info(f"OZ 뷰어 창 발견: {title}")
                return desktop.window(handle=w.handle)
        time.sleep(0.5)
    raise OzError("OZ 리포트 뷰어 창을 찾지 못했습니다(출력이 OZ 뷰어로 뜨지 않았거나 제목 키워드 불일치).")


def _print_to_pdf(oz_win, dest: Path, oz_cfg, logger, send_keys, Desktop):
    printer = oz_cfg.get("printer_name", "Microsoft Print to PDF")
    dlg_timeout = oz_cfg.get("dialog_timeout_sec", 15)

    # 1) 인쇄 대화상자 열기 (좌표 지정 시 그 버튼, 아니면 Ctrl+P)
    pos = oz_cfg.get("print_button_pos") or []
    if len(pos) == 2:
        import pyautogui
        x, y = oz_win.rectangle().left + pos[0], oz_win.rectangle().top + pos[1]
        pyautogui.click(x, y)
    else:
        send_keys("^p")
    logger.info("인쇄 대화상자 호출")

    # 부모(oz_win)의 자식으로서 인쇄 대화상자를 탐색
    print_dlg = _wait_dialog(Desktop, _PRINT_DIALOG_TITLES, dlg_timeout, parent_win=oz_win)
    if not print_dlg:
        raise OzError("인쇄 대화상자를 찾지 못했습니다.")

    # 2) 프린터를 'Microsoft Print to PDF' 로 선택
    _select_printer(print_dlg, printer, logger)

    # 파일로 인쇄 체크박스 처리
    _check_print_to_file(print_dlg, logger)

    # 3) 인쇄 실행
    if not _click_button(print_dlg, ["인쇄", "Print", "확인", "OK"]):
        send_keys("%p")  # Alt+P 폴백
    logger.info("인쇄 실행 → 저장 대화상자 대기")

    # 4) 저장 경로 입력
    # 저장 경로 지정 대화상자도 자식일 수 있으므로 parent_win=oz_win 지정
    save_dlg = _wait_dialog(Desktop, _SAVE_DIALOG_TITLES, dlg_timeout, parent_win=oz_win)
    if not save_dlg:
        raise OzError("'다른 이름으로 저장' 대화상자를 찾지 못했습니다.")
    _type_path_and_save(save_dlg, dest, logger, send_keys)

    # 5) 파일 생성 확인
    return _verify(dest)


def _save_via_viewer(oz_win, dest: Path, oz_cfg, logger, send_keys, Desktop):
    """OZ 뷰어 자체 '저장' 기능으로 PDF/Excel 등 파일로 저장."""
    send_keys("^s")
    logger.info("저장 설정 대화상자 호출 (Ctrl+S)")
    
    # 저장 설정 팝업 대화상자 대기
    dlg = _wait_dialog(Desktop, _SAVE_DIALOG_TITLES, oz_cfg.get("dialog_timeout_sec", 15), parent_win=oz_win)
    if not dlg:
        raise OzError("OZ 저장 설정 대화상자를 찾지 못했습니다.")
    
    file_format = oz_cfg.get("file_format", "PDF")
    logger.info(f"저장 설정 시작 (파일 형식: {file_format}, 출력 방향: Disk File)")

    # 1) 파일 형식 콤보박스(AutoID: 1205) 선택
    try:
        format_combo = dlg.child_window(auto_id="1205", control_type="ComboBox")
        if not _select_combo_item(format_combo, file_format, logger):
            logger.warning(f"파일 형식 콤보박스(1205)에서 '{file_format}' 매칭 선택 실패 (기본값 신뢰)")
    except Exception as e:
        logger.warning(f"파일 형식 콤보박스(1205) 접근 실패: {e}")

    # 2) 출력 방향 콤보박스(AutoID: 1206)에서 'Disk File' 선택
    try:
        direction_combo = dlg.child_window(auto_id="1206", control_type="ComboBox")
        if not _select_combo_item(direction_combo, "Disk File", logger):
            logger.warning("출력 방향 콤보박스(1206)에서 'Disk File' 매칭 선택 실패 (기본값 신뢰)")
    except Exception as e:
        logger.warning(f"출력 방향 콤보박스(1206) 접근 실패: {e}")

    # 3) 저장 경로 Edit(AutoID: 1209)에 경로 기입
    try:
        path_edit = dlg.child_window(auto_id="1209", control_type="Edit")
        path_edit.set_edit_text(str(dest))
        logger.info(f"저장 경로(1209) 입력 성공: {dest}")
    except Exception as edit_err:
        logger.warning(f"저장 경로(1209) 직접 입력 실패, type_keys 시도: {edit_err}")
        try:
            path_edit = dlg.child_window(auto_id="1209", control_type="Edit")
            path_edit.click_input()
            send_keys("^a{BACKSPACE}")
            send_keys(str(dest).replace(" ", "{SPACE}"))
            logger.info("저장 경로(1209) 키 입력 완료")
        except Exception as key_err:
            raise OzError(f"저장 경로(1209) 입력 불가능: {key_err}")

    # 4) 확인 버튼(AutoID: 1) 클릭하여 완료
    try:
        ok_btn = dlg.child_window(auto_id="1", control_type="Button")
        ok_btn.click_input()
        logger.info("저장 대화상자 '확인' 버튼 클릭")
    except Exception as btn_err:
        logger.warning(f"확인 버튼 클릭 실패, Enter 키 전송: {btn_err}")
        send_keys("{ENTER}")

    # 덮어쓰기 대화상자 대응
    time.sleep(0.5)
    send_keys("%y")

    return _verify(dest)


def _select_printer(print_dlg, printer_name, logger):
    """인쇄 대화상자에서 프린터 선택. UI 버전차로 실패해도 진행(기본 프린터 가정)."""
    try:
        for combo in print_dlg.descendants(control_type="ComboBox"):
            try:
                combo.select(printer_name)
                logger.info(f"프린터 선택: {printer_name}")
                return
            except Exception:
                continue
    except Exception:
        pass
    # 일부 Win10 인쇄 UI는 리스트(ListItem)
    try:
        item = print_dlg.child_window(title=printer_name, control_type="ListItem")
        item.select()
        logger.info(f"프린터 선택(목록): {printer_name}")
    except Exception:
        logger.info(f"프린터 자동 선택 실패 - 기본 프린터를 '{printer_name}'로 설정해 두는 것을 권장")


def _type_path_and_save(dlg, dest: Path, logger, send_keys):
    dlg.set_focus()
    edit = None
    try:
        edit = dlg.child_window(control_type="Edit", found_index=0)
    except Exception:
        pass
    if edit:
        try:
            edit.set_edit_text(str(dest))
        except Exception:
            edit.type_keys(str(dest), with_spaces=True)
    else:
        send_keys(str(dest).replace(" ", "{SPACE}"))
    time.sleep(0.3)
    if not _click_button(dlg, ["저장", "Save", "확인", "OK"]):
        send_keys("{ENTER}")
    logger.info(f"저장 경로 입력: {dest.name}")
    # 덮어쓰기 확인 대화상자 처리
    time.sleep(0.6)
    send_keys("%y")  # Alt+Y(예) - 있으면 동작, 없으면 무시됨


def _wait_dialog(Desktop, titles, timeout, parent_win=None):
    deadline = time.time() + timeout
    desktop = Desktop(backend="uia")
    while time.time() < deadline:
        # 1) 데스크톱 최상위 윈도우 검색
        for w in desktop.windows():
            try:
                t = w.window_text() or ""
                if any(t == title or title in t for title in titles):
                    return desktop.window(handle=w.handle)
            except Exception:
                continue

        # 2) 부모 윈도우가 전달되었을 시 그 자식 중에서 검색 (글자 깨짐 방어 포함)
        if parent_win:
            try:
                for w in parent_win.children():
                    try:
                        t = w.window_text() or ""
                        # 제목 일치
                        if any(t == title or title in t for title in titles):
                            return desktop.window(handle=w.handle)
                        # 인코딩 깨짐 대응: 윈도우 표준 대화상자(#32770) 클래스명 매칭
                        if w.element_info.class_name == "#32770" and w.element_info.control_type == "Window":
                            return desktop.window(handle=w.handle)
                    except Exception:
                        continue
            except Exception:
                pass
        time.sleep(0.4)
    return None


def _click_button(dlg, names):
    for name in names:
        try:
            btn = dlg.child_window(title=name, control_type="Button")
            if btn.exists():
                btn.click_input()
                return True
        except Exception:
            continue
    return False


def _verify(dest: Path) -> str:
    deadline = time.time() + 10
    while time.time() < deadline:
        if dest.exists() and dest.stat().st_size > 0:
            return str(dest)
        time.sleep(0.5)
    raise OzError(f"PDF 파일이 생성되지 않았습니다: {dest}")


def _check_print_to_file(print_dlg, logger):
    """인쇄 대화상자에서 '파일로 인쇄' 체크박스를 찾아 체크합니다."""
    try:
        checkboxes = print_dlg.descendants(control_type="CheckBox")
        for cb in checkboxes:
            try:
                txt = cb.window_text() or ""
                # '파일로 인쇄', 'Print to file' 등 정규화하여 매칭
                normalized_txt = txt.replace(" ", "").lower()
                if "파일로인쇄" in normalized_txt or "printtofile" in normalized_txt:
                    if not cb.is_checked():
                        try:
                            cb.check()
                        except Exception:
                            cb.click_input()
                        logger.info(f"'{txt}' 체크박스를 체크했습니다.")
                    else:
                        logger.info(f"'{txt}' 체크박스가 이미 체크되어 있습니다.")
                    return True
            except Exception as e:
                logger.debug(f"체크박스 개별 검사 오류: {e}")
                continue
    except Exception as e:
        logger.warning(f"체크박스 목록 획득 실패: {e}")

    logger.warning("'파일로 인쇄' 체크박스를 찾지 못했습니다.")
    return False


def _select_combo_item(combo, item_text, logger):
    """ComboBox를 확장하고 해당 텍스트를 포함하는 ListItem을 찾아 선택합니다."""
    # 1) select() 직접 호출 시도 (UIA가 바로 지원하는 경우)
    try:
        combo.select(item_text)
        logger.info(f"콤보박스 직접 선택 성공: '{item_text}'")
        return True
    except Exception:
        pass

    # 2) 목록을 열어서 ListItem을 탐색 후 선택
    try:
        combo.expand()
        time.sleep(0.3)
        for item in combo.descendants(control_type="ListItem"):
            t = item.window_text() or ""
            # 부분 일치(대소문자 무시)로 매칭
            if item_text.lower() in t.lower() or t.lower() in item_text.lower():
                item.select()
                logger.info(f"콤보박스 목록 탐색 선택 성공: '{t}' (타겟: '{item_text}')")
                return True
        combo.collapse()
    except Exception as e:
        logger.debug(f"콤보박스 목록 탐색 실패: {e}")

    # 3) 키보드 첫글자 매칭 입력 폴백 (Disk File -> d 입력 등)
    try:
        combo.click_input()
        time.sleep(0.2)
        first_char = item_text[0]
        from pywinauto.keyboard import send_keys
        send_keys(first_char.lower())
        time.sleep(0.2)
        send_keys("{ENTER}")
        logger.info(f"콤보박스 키보드 매치 시도 완료: '{item_text}'")
        return True
    except Exception:
        pass

    return False
