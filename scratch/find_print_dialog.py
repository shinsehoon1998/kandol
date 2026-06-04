from pywinauto import Desktop
import sys

def scan_windows():
    print("=== Filtered Scan (UIA) ===")
    d_uia = Desktop(backend="uia")
    for w in d_uia.windows():
        try:
            title = w.window_text() or ""
            class_name = w.element_info.class_name or ""
            keywords = ["인쇄", "print", "save", "저장", "oz", "오즈", "뷰어", "viewer", "report", "리포트", "dialog", "다이얼로그", "alpdf"]
            if any(k in title.lower() for k in keywords) or any(k in class_name.lower() for k in keywords):
                print(f"[UIA] Title: '{title}', Class: '{class_name}', Visible: {w.is_visible()}, Enabled: {w.is_enabled()}")
                
                if "viewer" in title.lower() or "리포트" in title.lower() or "인쇄" in title.lower() or "print" in title.lower() or "alpdf" in title.lower():
                    for child in w.children():
                        try:
                            ct = child.window_text() or ""
                            cc = child.element_info.class_name or ""
                            c_type = child.element_info.control_type or ""
                            print(f"   -> Child Title: '{ct}', Class: '{cc}', ControlType: '{c_type}'")
                        except Exception:
                            pass
        except Exception as e:
            pass

if __name__ == "__main__":
    scan_windows()
