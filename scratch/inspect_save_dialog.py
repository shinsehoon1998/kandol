from pywinauto import Desktop
import sys

def inspect_dialogs():
    print("=== OZ Viewer Save Dialog Inspector ===")
    d_uia = Desktop(backend="uia")
    oz_win = None
    
    # 1) OZ 뷰어 메인 창 찾기
    for w in d_uia.windows():
        try:
            title = w.window_text() or ""
            class_name = w.element_info.class_name or ""
            if "oz" in title.lower() or "오즈" in title.lower() or "viewer" in title.lower() or "리포트" in title.lower() or class_name == "OZReportViewerMainFrame3.0":
                oz_win = w
                print(f"Found OZ Viewer Main Window: '{title}' (Class: {class_name})")
                break
        except Exception:
            pass
            
    if not oz_win:
        print("OZ 뷰어 메인 창을 찾지 못했습니다.")
        print("\n--- Scanning all #32770 windows on desktop ---")
        for w in d_uia.windows():
            try:
                if w.element_info.class_name == "#32770" or "저장" in w.window_text() or "save" in w.window_text().lower() or "export" in w.window_text().lower():
                    print(f"Top Window Title: '{w.window_text()}', Class: {w.element_info.class_name}")
                    print_control_tree(w)
            except Exception:
                pass
        return

    # 2) OZ 메인 창의 자식들 스캔
    print("\n--- Scanning children of OZ Viewer ---")
    for child in oz_win.children():
        try:
            title = child.window_text() or ""
            class_name = child.element_info.class_name or ""
            control_type = child.element_info.control_type or ""
            print(f"Child Window - Title: '{title}', Class: '{class_name}', ControlType: '{control_type}'")
            
            if class_name == "#32770" or control_type == "Window" or "저장" in title or "내보내기" in title or "export" in title.lower() or "save" in title.lower():
                print(f"\n>>> Printing Control Tree for '{title}' ({class_name}):")
                print_control_tree(child)
        except Exception as e:
            print(f"Error scanning child: {e}")
            pass

def print_control_tree(win, depth=0):
    try:
        title = win.window_text() or ""
        control_type = win.element_info.control_type or ""
        class_name = win.element_info.class_name or ""
        auto_id = win.element_info.automation_id or ""
        print("  " * depth + f"- Type: '{control_type}', Title: '{title}', Class: '{class_name}', AutoID: '{auto_id}'")
        
        for child in win.children():
            print_control_tree(child, depth + 1)
    except Exception as e:
        pass

if __name__ == "__main__":
    inspect_dialogs()
