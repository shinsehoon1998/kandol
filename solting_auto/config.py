"""설정 로딩. config.yaml + 환경변수(.env 선택)."""

import os
from pathlib import Path

import yaml


def _load_dotenv(path: Path) -> None:
    """간이 .env 로더 (외부 의존성 없이). KEY=VALUE 형식만 지원."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def load_config(config_path: str = "config.yaml") -> dict:
    base = Path.cwd()
    _load_dotenv(base / ".env")

    cfg_file = Path(config_path)
    if not cfg_file.exists():
        raise FileNotFoundError(
            f"설정 파일이 없습니다: {config_path}\n"
            f"config.example.yaml 을 복사해 config.yaml 을 만드세요."
        )
    with cfg_file.open(encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # 비밀번호 해석: password_env -> 환경변수, 없으면 password 직접값
    _resolve_password(cfg.setdefault("credentials", {}))
    # 보험사(2단계) 자격증명도 동일하게 해석
    if isinstance(cfg.get("insurance"), dict):
        _resolve_password(cfg["insurance"].setdefault("credentials", {}))

    return cfg


def _resolve_password(creds: dict) -> None:
    """password_env(환경변수) 또는 password(직접값)에서 비밀번호를 런타임 키에 채운다."""
    pw = None
    env_key = creds.get("password_env")
    if env_key:
        pw = os.environ.get(env_key)
    if not pw:
        pw = creds.get("password")
    creds["_resolved_password"] = pw  # 런타임 전용(파일에 다시 쓰지 않음)
