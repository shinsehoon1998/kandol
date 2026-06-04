"""중복 판별. (PRD FR-4.2) 기준 = 전화번호(정규화 후 비교).

- 파일 내 중복: 같은 파일에서 동일 전화번호가 2번 이상이면 두 번째부터 Skip
- 기등록 중복: 이전에 성공 등록된 전화번호 목록(seen_store)과 대조
"""

import json
from pathlib import Path

from .validators import normalize_phone


class PhoneDedup:
    def __init__(self, store_path: str = None):
        """store_path: 누적 등록 전화번호 저장 파일(JSON). None이면 메모리만 사용."""
        self.store_path = Path(store_path) if store_path else None
        self._registered = set()      # 기등록(영속)
        self._seen_in_run = set()     # 이번 실행에서 본 번호
        if self.store_path and self.store_path.exists():
            try:
                self._registered = set(json.loads(self.store_path.read_text(encoding="utf-8")))
            except Exception:
                self._registered = set()

    def is_duplicate(self, phone) -> bool:
        key = normalize_phone(phone)
        if not key:
            return False
        return key in self._registered or key in self._seen_in_run

    def mark_seen(self, phone) -> None:
        """이번 실행에서 처리(시도)한 번호로 표시 - 파일 내 중복 방지."""
        key = normalize_phone(phone)
        if key:
            self._seen_in_run.add(key)

    def mark_registered(self, phone) -> None:
        """성공 등록된 번호로 영속 기록."""
        key = normalize_phone(phone)
        if key:
            self._registered.add(key)

    def save(self) -> None:
        if not self.store_path:
            return
        self.store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store_path.write_text(
            json.dumps(sorted(self._registered), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
