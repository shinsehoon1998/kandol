# -*- mode: python ; coding: utf-8 -*-
# 깐돌이 에이전트 PyInstaller 설정 (재작성본)
# 원본 spec 이 저장소에 없어서 v1.9.13 릴리즈 zip 의 구성(Kkandori.exe + _internal, 창 모드,
# config.example.yaml + docs 번들, playwright driver 포함)에 맞춰 다시 만들었다.
from PyInstaller.utils.hooks import collect_all

datas = [("config.example.yaml", "."), ("docs", "docs")]
binaries = []
hiddenimports = ["fitz", "yaml", "dotenv"]

# 데이터·드라이버 파일까지 필요한 패키지는 통째로 수집 (설치돼 있지 않으면 건너뜀)
for pkg in ("playwright", "supabase", "pywinauto", "comtypes", "pyautogui"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as exc:  # noqa: BLE001
        print(f"[spec] collect_all({pkg}) 건너뜀: {exc}")

a = Analysis(
    ["solting_auto/agent_app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Kkandori",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # 창(GUI) 앱
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Kkandori",
)
