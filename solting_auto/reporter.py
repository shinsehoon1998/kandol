"""처리 결과 리포트 생성. (PRD FR-5.1, 8.2)

다단계(솔팅 → 보험사) 결과를 단계별로 기록. 주민번호는 마스킹하여 출력한다.
"""

from dataclasses import dataclass, field
from pathlib import Path

from openpyxl import Workbook

from .masking import mask_jumin


# 상태 상수
SUCCESS = "성공"
FAIL = "실패"
SKIP = "Skip"

# 중복 스킵 사유 접두어 — 판단 출처(로컬 저장 / KB 전산 팝업)를 명확히 구분
REASON_DUP_LOCAL = "기등록(로컬)"   # 이 PC 깐돌이가 과거 성공 등록한 번호(로컬 저장소 기준)
REASON_DUP_KB = "기등록(KB)"        # KB 전산이 실시간 중복 팝업으로 알려준 경우


@dataclass
class RowResult:
    row_no: int
    jumin: str
    name: str
    phone: str
    status: str                 # 전체 상태(성공/실패/Skip)
    address: str = ""           # 주소지(고객DB 저장용)
    reason: str = ""            # 전체 사유(검증/중복 Skip 등)
    timestamp: str = ""
    # 단계별 결과
    solting_status: str = ""
    solting_reason: str = ""
    insurance_status: str = ""
    insurance_reason: str = ""
    consent_pdf: str = ""       # 동의서 PDF 경로
    consent_stamped_pdf: str = "" # 서명/스탬프 완료된 PDF 경로
    kb_scan_status: str = ""    # KB스캔 상태 (성공/실패/빈값)
    kb_scan_reason: str = ""    # KB스캔 실패 사유
    screenshot: str = ""


@dataclass
class ReportSummary:
    total: int = 0
    success: int = 0
    fail: int = 0
    skip: int = 0
    dup_local: int = 0          # 기등록 스킵 - 로컬 저장소 기준
    dup_kb: int = 0             # 기등록 스킵 - KB 팝업 기준
    etc_skip: int = 0           # 그 외 스킵(검증 실패 등)
    consent_count: int = 0      # 동의서 PDF 발급 수
    kb_scan_count: int = 0     # KB스캔 전송 완료 수
    results: list = field(default_factory=list)

    def add(self, r: RowResult):
        self.results.append(r)
        self.total += 1
        if r.status == SUCCESS:
            self.success += 1
        elif r.status == FAIL:
            self.fail += 1
        elif r.status == SKIP:
            self.skip += 1
            rs = r.reason or ""
            if rs.startswith(REASON_DUP_LOCAL):
                self.dup_local += 1
            elif rs.startswith(REASON_DUP_KB):
                self.dup_kb += 1
            else:
                self.etc_skip += 1
        if r.consent_pdf:
            self.consent_count += 1
        if r.kb_scan_status == "성공":
            self.kb_scan_count += 1

    def as_text(self) -> str:
        return (
            f"총 {self.total}건 | ✅신규등록 {self.success} | "
            f"⏭️기등록(로컬) {self.dup_local} | ⏭️기등록(KB) {self.dup_kb} | "
            f"⚠️검증스킵 {self.etc_skip} | ❌실패 {self.fail} | 동의서 {self.consent_count}"
        )


def write_report(summary: ReportSummary, output_folder: str, source_name: str, timestamp: str) -> str:
    """결과 엑셀 리포트 저장. 주민번호는 마스킹. 반환=저장 경로."""
    Path(output_folder).mkdir(parents=True, exist_ok=True)
    safe_ts = timestamp.replace(":", "").replace(" ", "_")
    out_path = Path(output_folder) / f"result_{source_name}_{safe_ts}.xlsx"

    wb = Workbook()
    ws = wb.active
    ws.title = "결과"
    ws.append([
        "행", "주민번호(마스킹)", "이름", "전화번호", "전체상태",
        "솔팅상태", "솔팅사유", "보험사상태", "보험사사유", "동의서PDF", "서명완료동의서PDF", "KB스캔상태", "KB스캔사유", "처리시각",
    ])
    for r in summary.results:
        ws.append([
            r.row_no, mask_jumin(r.jumin), r.name, r.phone, r.status,
            r.solting_status, r.solting_reason,
            r.insurance_status, r.insurance_reason,
            Path(r.consent_pdf).name if r.consent_pdf else "",
            Path(r.consent_stamped_pdf).name if r.consent_stamped_pdf else "",
            r.kb_scan_status, r.kb_scan_reason,
            r.timestamp,
        ])

    ws2 = wb.create_sheet("요약")
    ws2.append(["총건수", summary.total])
    ws2.append(["신규등록(성공)", summary.success])
    ws2.append(["기등록 스킵(로컬)", summary.dup_local])
    ws2.append(["기등록 스킵(KB)", summary.dup_kb])
    ws2.append(["검증/기타 스킵", summary.etc_skip])
    ws2.append(["실패", summary.fail])
    ws2.append(["Skip 합계", summary.skip])
    ws2.append(["동의서 발급", summary.consent_count])
    ws2.append(["KB스캔 완료", summary.kb_scan_count])

    wb.save(out_path)
    return str(out_path)
