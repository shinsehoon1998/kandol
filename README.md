# 솔팅프로그램 전산등록 자동화 매크로 (PoC)

엑셀(주민번호·이름·전화번호) 파일을 읽어 솔팅프로그램 웹 폼에 자동 등록하는 매크로.
PRD: [PRD_전산등록_자동화매크로.md](PRD_전산등록_자동화매크로.md)

> **현재 상태**: PoC. 검증·중복·리포트·로깅·마스킹은 완성·동작 검증됨.
> 브라우저 자동화는 골격 완성 상태이며, **솔팅프로그램 실제 화면 분석 후 `config.yaml` 의 `selectors` 만 채우면 동작**합니다.

## 처리 흐름 (2단계 파이프라인)

```
엑셀 읽기 → 행별 검증 → [1단계 솔팅 등록] → [2단계 보험사 등록 + 동의서 PDF] → 결과기록 → 리포트
                              (각 단계: 전화번호 중복체크 후 등록)
```

- **1단계(solting)**: 솔팅프로그램 전산등록
- **2단계(insurance)**: 보험사 전산 고객등록 + 사이트의 출력/다운로드 버튼으로 **동의서 PDF 저장**
- 단계는 `config.yaml > stages` 또는 웹 화면 체크박스로 on/off
- **검증**: 주민번호 13자리, 전화번호 형식, 이름 공백 (FR-4.1)
- **중복**: 전화번호 정규화 후 비교 → 중복 시 Skip. **단계별 독립 저장소** (FR-4.2)
- **보안**: 주민번호/전화번호를 화면·로그·리포트에서 마스킹. 동의서 PDF 파일명에 주민번호 미포함 (PRD 7.1)
- 동의서 PDF 저장 위치: `output/consent_pdfs/동의서_{이름}_{전화뒤4}.pdf`

## 설치

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium          # 실제 등록 시 필요 (dry-run 은 불필요)
```

## 설정

```bash
cp config.example.yaml config.yaml   # 셀렉터/URL/계정 입력
cp .env.example .env                 # SOLTING_PASSWORD 입력
```

`config.yaml` 의 `selectors.*` 에 솔팅프로그램 실제 요소 셀렉터를 채웁니다.
비밀번호는 `.env` 의 `SOLTING_PASSWORD` 환경변수로 주입됩니다(평문 저장 방지).

## 실행 (A) 웹 — 권장, 맥미니 배포에 적합

브라우저에서 엑셀을 업로드하고 진행률·결과를 확인하는 방식.

```bash
# 맥: Finder 에서 run_web.command 더블클릭 (최초 1회 자동 설치 후 브라우저 자동 실행)

# 또는 터미널에서
python web/app.py                              # http://localhost:8000
HOST=0.0.0.0 PORT=8000 python web/app.py        # 사내망 다른 PC에서도 접속 허용
```

| 화면 | 기능 |
|---|---|
| 엑셀 드래그&드롭 업로드 | .xlsx 선택 |
| "미리검증(dry-run)" 체크 | 실제 등록 없이 검증·중복만 확인 |
| 진행률 바 + 실시간 행 상태 | 처리 진행 표시 |
| 결과 표(주민번호·전화 마스킹) + 리포트 다운로드 | 성공/실패/Skip |

## 실행 (B) CLI

```bash
# 브라우저 없이 검증/중복/리포트만 (셀렉터 없이도 동작 - 데이터 점검용)
python main.py --file input/test.xlsx --dry-run

# 단일 파일 실제 등록
python main.py --file input/test.xlsx

# 감시 폴더(input/)의 엑셀 전체 처리 → 완료/오류 폴더로 이동
python main.py --watch
```

## 결과물 (output/)

| 파일 | 내용 |
|---|---|
| `result_*.xlsx` | 행별 결과(성공/실패/Skip + 사유), **주민번호 마스킹** + 요약 시트 |
| `run.log` | 실행 로그 (민감정보 자동 마스킹) |
| `screenshots/` | 실패 건 증빙 스크린샷 |
| `registered_phones.json` | 누적 등록 전화번호(중복 판별용) |

## 구조

```
solting_auto/
  config.py        설정 + .env 로딩, 비밀번호 환경변수 해석
  excel_reader.py  고정 양식 엑셀 파싱
  validators.py    주민번호/이름/전화 검증·정규화
  dedup.py         전화번호 기준 중복 판별(파일내 + 기등록)
  masking.py       주민번호/전화/이름 마스킹
  logger.py        마스킹 적용 로거
  automation.py    Playwright 로그인→등록→결과판별 (셀렉터 주입)
  reporter.py      결과 엑셀 리포트(마스킹)
  runner.py        파이프라인 오케스트레이션 (진행률 콜백 지원)
  insurance.py     KB손해보험 동의서출력 (로그인/팝업/출력) + PDF 캡처
  oz_viewer.py     OZ 리포트 뷰어 → PDF 저장 (Windows 전용, pywinauto)
web/
  app.py           Flask 웹 서버 (업로드/진행률/결과/다운로드)
  templates/index.html   업로드 UI
tools/test_oz_pdf.py     OZ→PDF 저장 단독 테스트 (Windows 튜닝용)
main.py            CLI 진입점 (--file / --watch / --dry-run)
run_web.command    맥 더블클릭 실행 런처
run_web.bat        Windows 실행 런처
start_edge_debug.bat   Windows: 수동 로그인용 디버그 Edge 실행
docs/KB_동의서출력_조사가이드.md   셀렉터/PDF 엔드포인트 조사
docs/KB_Windows_설치가이드.md      Windows 2단계 설치·운영
```

## 실행 환경 요약

| 작업 | 환경 |
|---|---|
| 1단계 솔팅 등록, 엑셀 검증/중복, 리포트 | 맥미니 / Windows 모두 가능 |
| 2단계 KB 동의서 PDF (OZ 리포트 뷰어 제어) | **Windows 필요** — `docs/KB_Windows_설치가이드.md` 참조 |

## M0 이후 채워야 할 것 (PRD 12.2 잔여 이슈)

1. `config.yaml > selectors` — 로그인/등록 폼 실제 셀렉터
2. `config.yaml > site` — 로그인/등록 URL
3. `format.jumin_hyphen` / `phone_hyphen` — 솔팅프로그램이 요구하는 입력 포맷
4. CAPTCHA/추가 인증 존재 시 수동 개입 로직 보강
5. 주민번호 보관 기간·파기 정책(사내 개인정보 처리방침 연계)

## 보안 주의 (개인정보)

- 주민번호는 **고유식별정보**입니다. `config.yaml`, `.env`, `input/`, `output/` 는 `.gitignore` 처리되어 있습니다.
- `registered_phones.json` 은 중복 판별을 위해 전화번호를 로컬 저장합니다. 접근 권한을 제한하세요.
- 처리 완료 후 원본 엑셀·임시 데이터 파기 정책을 운영 절차에 포함하세요.
