"""입력값 검증 및 정규화. (PRD FR-4.1)

- 주민번호: 13자리 형식 + (옵션) 체크섬
- 전화번호: 숫자만 정규화 (중복 판별 키)
- 이름: 공백/빈값 검사
"""

import re
from dataclasses import dataclass


@dataclass
class FieldResult:
    ok: bool
    reason: str = ""


def normalize_digits(value) -> str:
    """숫자만 남긴다. None/숫자/문자 모두 처리."""
    if value is None:
        return ""
    return re.sub(r"\D", "", str(value).strip())


def normalize_phone(value) -> str:
    """전화번호 정규화 - 숫자만. 중복 판별/입력에 공통 사용."""
    return normalize_digits(value)


def validate_jumin(value, use_checksum: bool = False) -> FieldResult:
    digits = normalize_digits(value)
    if not digits:
        return FieldResult(False, "주민번호 누락")
    if len(digits) != 13:
        return FieldResult(False, f"주민번호 자리수 오류({len(digits)}자리)")
    if use_checksum and not _jumin_checksum_ok(digits):
        return FieldResult(False, "주민번호 체크섬 불일치")
    return FieldResult(True)


def validate_name(value) -> FieldResult:
    name = "" if value is None else str(value).strip()
    if not name:
        return FieldResult(False, "이름 누락")
    return FieldResult(True)


def validate_phone(value) -> FieldResult:
    digits = normalize_phone(value)
    if not digits:
        return FieldResult(False, "전화번호 누락")
    if not (9 <= len(digits) <= 11):
        return FieldResult(False, f"전화번호 자리수 오류({len(digits)}자리)")
    return FieldResult(True)


def _jumin_checksum_ok(digits: str) -> bool:
    """주민등록번호 검증식 (표준 가중치). 2000년대 신규번호 체계는 예외 있음."""
    weights = [2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5]
    total = sum(int(d) * w for d, w in zip(digits[:12], weights))
    check = (11 - (total % 11)) % 10
    return check == int(digits[12])
