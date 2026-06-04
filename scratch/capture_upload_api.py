import time
import sys
import os
from playwright.sync_api import sync_playwright

LOG_PATH = "output/captured_upload_api.txt"

def log_request(request):
    try:
        url = request.url
        method = request.method
        
        # chrome-extension 또는 static assets 제외
        if "chrome-extension://" in url or url.endswith((".js", ".css", ".png", ".gif", ".jpg", ".ico", ".woff", ".woff2")):
            return
            
        # POST/PUT 요청 또는 edms/upload 관련 키워드가 포함된 요청만 집중 필터링
        keywords = ["upload", "edms", "transfer", "send", "save", "scan", "post", "sso"]
        is_target = method in ["POST", "PUT"] or any(kw in url.lower() for kw in keywords)
        
        if is_target:
            print(f"[{method}] 요청 감지: {url}")
            headers = request.headers
            post_data = request.post_data
            
            # Post Data 크기 포맷팅
            data_len = len(post_data) if post_data else 0
            
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(f"\n==================================================\n")
                f.write(f"시간: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"Method: {method}\n")
                f.write(f"URL: {url}\n")
                f.write(f"Headers:\n")
                for k, v in headers.items():
                    f.write(f"  {k}: {v}\n")
                if post_data:
                    f.write(f"Post Data (Size: {data_len} bytes):\n")
                    # 멀티파트 바디나 너무 긴 데이터는 앞부분만 잘라서 보여주되, multipart 바운더리 등 헤더 분석용으로 로깅
                    if data_len > 2000:
                        f.write(post_data[:2000] + "\n... (데이터 길어서 생략) ...\n")
                    else:
                        f.write(post_data + "\n")
                else:
                    f.write("Post Data: None\n")
                f.write(f"==================================================\n")
    except Exception as e:
        print(f"요청 로깅 중 오류: {e}")

def log_response(response):
    try:
        url = response.url
        if "chrome-extension://" in url or url.endswith((".js", ".css", ".png", ".gif", ".jpg", ".ico", ".woff", ".woff2")):
            return
            
        if response.status >= 400:
            print(f"[응답 경고] {response.status} {response.status_text} : {url}")
        elif "upload" in url.lower() or "edms" in url.lower():
            print(f"[응답 감지] {response.status} : {url}")
            try:
                text = response.text()
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(f"\n--- RESPONSE ({response.status}) ---\n")
                    f.write(f"URL: {url}\n")
                    f.write(f"Content:\n{text[:1000]}\n")
                    f.write(f"-----------------------------------\n")
            except:
                pass
    except Exception as e:
        pass

def main():
    os.makedirs("output", exist_ok=True)
    # 기존 로그 파일 삭제/초기화
    if os.path.exists(LOG_PATH):
        try:
            os.remove(LOG_PATH)
        except:
            pass
            
    print("Edge 브라우저(CDP Port 9222) 연결 중...")
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp("http://localhost:9222")
            print("성공적으로 브라우저에 연결되었습니다.")
        except Exception as e:
            print(f"[오류] 브라우저 연결 실패: {e}")
            print("Edge 브라우저가 디버그 모드(start_edge_debug.bat)로 열려 있는지 확인하세요.")
            sys.exit(1)
            
        print("\n--- 현재 열려 있는 탭 목록 ---")
        for context in browser.contexts:
            for page in context.pages:
                print(f" - URL: {page.url} | 제목: {page.title()}")
                # 모든 페이지에 요청/응답 리스너 연결
                page.on("request", log_request)
                page.on("response", log_response)
                
        print("\n[대기 상태] 모니터링이 시작되었습니다.")
        print(f"로그는 '{LOG_PATH}' 파일에 실시간 누적 기록됩니다.")
        print("이제 EDMS 화면으로 가셔서 이미지 추가 및 [전송]을 직접 실행해 주세요.")
        print("모니터링을 종료하려면 Ctrl+C를 누르세요.\n")
        
        try:
            while True:
                # 탭이 새로 열릴 수도 있으므로 신규 탭 모니터링 체크
                for context in browser.contexts:
                    for page in context.pages:
                        # 이미 리스너가 걸린 페이지는 무시하고, 새로 열린 페이지가 있으면 걸어줍니다.
                        # 플레이라이트의 리스너 목록 조회가 어려우므로, 모든 신규 페이지에 중복 제거 처리를 위해 try-except 활용
                        try:
                            page.remove_listener("request", log_request)
                        except:
                            pass
                        try:
                            page.remove_listener("response", log_response)
                        except:
                            pass
                        page.on("request", log_request)
                        page.on("response", log_response)
                time.sleep(2.0)
        except KeyboardInterrupt:
            print("\n모니터링이 사용자에 의해 중단되었습니다.")

if __name__ == "__main__":
    main()
