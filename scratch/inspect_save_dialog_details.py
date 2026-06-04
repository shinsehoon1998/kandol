from pywinauto import Desktop
import time

def inspect_combos():
    d_uia = Desktop(backend="uia")
    dlg = None
    for w in d_uia.windows():
        try:
            if w.element_info.class_name == "#32770" and w.parent().element_info.class_name == "OZReportViewerMainFrame3.0":
                dlg = w
                break
        except Exception:
            pass
            
    if not dlg:
        print("대화상자를 찾지 못했습니다. 데스크톱 전체에서 탐색합니다.")
        for w in d_uia.windows():
            try:
                if w.element_info.class_name == "#32770":
                    dlg = w
                    break
            except Exception:
                pass
                
    if not dlg:
        print("대화상자를 찾지 못했습니다.")
        return
        
    print(f"Inspecting dialog: '{dlg.window_text()}'")
    for combo_id in ["1205", "1206"]:
        try:
            combo = dlg.child_window(auto_id=combo_id, control_type="ComboBox")
            print(f"\n--- ComboBox {combo_id} ---")
            
            # 현재 선택값 출력 시도
            try:
                curr = combo.window_text() or ""
                print(f"Window Text: '{curr}'")
            except Exception:
                pass
                
            # 확장하여 아이템 텍스트 목록 출력
            try:
                combo.expand()
                time.sleep(0.5)
                items = combo.descendants(control_type="ListItem")
                print(f"Found {len(items)} items:")
                for item in items:
                    print(f"  Item: '{item.window_text()}'")
                combo.collapse()
            except Exception as e:
                print(f"Expand failed: {e}")
        except Exception as e:
            print(f"Combo {combo_id} error: {e}")

if __name__ == "__main__":
    inspect_combos()
