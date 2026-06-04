import sys
import os
from playwright.sync_api import sync_playwright

OUT_PATH = "output/edms_html_dump.txt"

def dump_frame_content(f, frame, index, prefix=""):
    try:
        url = frame.url
        name = frame.name or f"UnnamedFrame_{index}"
        f.write(f"\n{prefix}=== FRAME [{index}]: Name='{name}' | URL='{url}' ===\n")
        
        try:
            content = frame.content()
            f.write(content + "\n")
        except Exception as ce:
            f.write(f"[오류] 프레임 콘텐츠 획득 실패: {ce}\n")
            
        # 재귀적으로 자식 프레임도 덤프
        for i, child in enumerate(frame.child_frames):
            dump_frame_content(f, child, i, prefix + "  ")
            
    except Exception as e:
        f.write(f"[오류] 프레임 [{index}] 덤프 중 오류: {e}\n")

def main():
    os.makedirs("output", exist_ok=True)
    if os.path.exists(OUT_PATH):
        try:
            os.remove(OUT_PATH)
        except:
            pass
            
    print("Edge 브라우저(CDP Port 9222) 연결 중...")
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
            print("성공적으로 브라우저에 연결되었습니다.")
        except Exception as e:
            print(f"[오류] 브라우저 연결 실패: {e}")
            sys.exit(1)
            
        found_target = False
        with open(OUT_PATH, "w", encoding="utf-8") as f:
            f.write("=== KB손해보험 EDMS HTML/DOM Source Dump ===\n")
            f.write(f"덤프 시간: {time.strftime('%Y-%m-%d %H:%M:%S') if 'time' in sys.modules else ''}\n\n")
            
            for c_idx, context in enumerate(browser.contexts):
                for p_idx, page in enumerate(context.pages):
                    url = page.url
                    title = page.title()
                    print(f"검사 중: URL='{url}' | 제목='{title}'")
                    
                    # EDMS 또는 scanStation 관련 페이지 감지
                    is_edms = "edms" in url.lower() or "scan" in url.lower() or "xfs" in url.lower() or "kbinsure" in url.lower()
                    
                    f.write(f"==================================================\n")
                    f.write(f"PAGE [{c_idx}-{p_idx}]: Title='{title}' | URL='{url}' (EDMS 감지: {is_edms})\n")
                    f.write(f"==================================================\n")
                    
                    try:
                        content = page.content()
                        f.write(content + "\n")
                        found_target = True
                    except Exception as pe:
                        f.write(f"[오류] 페이지 콘텐츠 획득 실패: {pe}\n")
                        
                    # 프레임 구조 탐색 및 덤프
                    for f_idx, frame in enumerate(page.frames):
                        if frame != page.main_frame:
                            dump_frame_content(f, frame, f_idx, "  ")
                            
        if found_target:
            print(f"\n[성공] EDMS 페이지 DOM 소스가 '{OUT_PATH}' 파일에 저장되었습니다.")
            print("이제 소스코드 분석을 시작합니다.")
        else:
            print("\n[경고] 저장할 대상 페이지 콘텐츠를 찾지 못했습니다.")

if __name__ == "__main__":
    import time
    main()
