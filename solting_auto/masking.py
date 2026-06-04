"""개인정보 마스킹 유틸. (PRD 7.1 - 주민번호 등 민감정보 보호)

화면/로그/리포트/스크린샷 등 모든 출력 경로에서 사용한다.
"""

import re


def mask_jumin(jumin: str) -> str:
    """주민번호 뒷자리 마스킹. 900115-1234567 -> 900115-1******

    하이픈 유무와 무관하게 앞 7자리(생년월일 6 + 성별 1)만 남기고 마스킹한다.
    """
    if not jumin:
        return ""
    digits = re.sub(r"\D", "", str(jumin))
    if len(digits) < 7:
        # 형식이 비정상이면 전부 마스킹
        return "*" * len(digits)
    head = digits[:7]  # 생년월일 6 + 성별 1
    masked = head + "*" * (len(digits) - 7)
    # 표준 표기로 하이픈 삽입 (6 + 7)
    return f"{masked[:6]}-{masked[6:]}"


def mask_phone(phone: str) -> str:
    """전화번호 가운데 마스킹. 010-1234-5678 -> 010-****-5678"""
    if not phone:
        return ""
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) <= 7:
        return phone
    # 마지막 4자리, 앞 3자리만 노출
    head = digits[:3]
    tail = digits[-4:]
    return f"{head}-****-{tail}"


def mask_name(name: str) -> str:
    """이름 가운데 마스킹. 홍길동 -> 홍*동, 김철 -> 김*"""
    if not name:
        return ""
    name = str(name)
    if len(name) <= 1:
        return name
    if len(name) == 2:
        return name[0] + "*"
    return name[0] + "*" * (len(name) - 2) + name[-1]
