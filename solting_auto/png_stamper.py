import os
import random
import struct
from PIL import Image, ImageDraw
from PIL.PngImagePlugin import PngInfo


def _save_png_preserving(img, out_path, orig_info, logger=None):
    """스탬프된 PNG를 저장하되 원본의 메타데이터(DPI/pHYs, sRGB, gAMA)를 그대로 유지한다.
    KB EDMS 스캔은 DPI로 문서의 물리적 비율을 판정하므로 DPI가 사라지면 비율을 다르게
    해석해 인식이 실패한다. DPI(필수) 외에 sRGB/gAMA 색공간 청크도 원본과 일치시켜
    스탬프 파일이 원본과 메타데이터상 동일해지도록 한다.
    """
    info = orig_info or {}
    save_kwargs = {}

    # 1) DPI(pHYs) — 비율 인식의 핵심
    dpi = info.get("dpi")
    if dpi:
        save_kwargs["dpi"] = dpi

    # 2) sRGB / gAMA 색공간 청크 복원 (원본과 메타데이터 완전 일치)
    try:
        meta = PngInfo()
        added = False
        if "srgb" in info:
            meta.add(b"sRGB", bytes([int(info["srgb"]) & 0xFF]))
            added = True
        if "gamma" in info:
            meta.add(b"gAMA", struct.pack(">I", int(round(float(info["gamma"]) * 100000))))
            added = True
        if added:
            save_kwargs["pnginfo"] = meta
    except Exception as meta_err:
        if logger:
            logger.warning(f"[스탬프] sRGB/gAMA 복원 생략(무시): {meta_err}")

    img.save(out_path, format="PNG", **save_kwargs)
    if logger:
        logger.info(f"[스탬프] PNG 저장(메타 보존 dpi={dpi}, srgb={info.get('srgb')}, gamma={info.get('gamma')}): "
                    f"{os.path.basename(out_path)}")


def stamp_single_png_set(page_paths: list, output_paths: list, logger) -> bool:
    """다운로드받은 동의서 PNG 이미지 파일 세트(페이지별 이미지)에 동의함 체크표시 및 서명을 주입하여 다른 폴더에 저장합니다.
    page_paths: [page1_path, page2_path, page3_path]
    output_paths: [out1_path, out2_path, out3_path]
    """
    saved_count = 0
    try:
        # 1페이지 처리
        if len(page_paths) >= 1 and os.path.exists(page_paths[0]):
            img = Image.open(page_paths[0])
            orig_info = dict(img.info)   # 원본 메타데이터(DPI 등) 캡처
            w, h = img.size
            # PDF 기준 크기 (A4: 595 x 842) 대비 스케일 계산
            sx, sy = w / 595.0, h / 842.0

            draw = ImageDraw.Draw(img)
            # 고유식별정보, 민감정보, 개인(신용)정보 수집/이용 동의
            p1_coords = [(515, 315), (515, 355), (515, 490)]
            for cx, cy in p1_coords:
                _draw_checkmark(draw, cx * sx, cy * sy, sx, sy)
            os.makedirs(os.path.dirname(output_paths[0]), exist_ok=True)
            _save_png_preserving(img, output_paths[0], orig_info, logger)
            saved_count += 1
            logger.info(f"PNG 1페이지 동의함 체크 완료: {os.path.basename(output_paths[0])}")
        elif len(page_paths) >= 1:
            logger.warning(f"PNG 1페이지 입력 파일이 없어 건너뜁니다: {page_paths[0]}")

        # 2페이지 처리
        if len(page_paths) >= 2 and os.path.exists(page_paths[1]):
            img = Image.open(page_paths[1])
            orig_info = dict(img.info)   # 원본 메타데이터(DPI 등) 캡처
            w, h = img.size
            sx, sy = w / 595.0, h / 842.0

            draw = ImageDraw.Draw(img)
            # 고유식별정보, 민감정보, 개인(신용)정보 제공 동의, 국외 제공 동의
            p2_coords = [(515, 215), (515, 265), (515, 375), (490, 720)]
            for cx, cy in p2_coords:
                _draw_checkmark(draw, cx * sx, cy * sy, sx, sy)
            os.makedirs(os.path.dirname(output_paths[1]), exist_ok=True)
            _save_png_preserving(img, output_paths[1], orig_info, logger)
            saved_count += 1
            logger.info(f"PNG 2페이지 동의함 체크 완료: {os.path.basename(output_paths[1])}")
        elif len(page_paths) >= 2:
            logger.warning(f"PNG 2페이지 입력 파일이 없어 건너뜁니다: {page_paths[1]}")

        # 3페이지 처리
        if len(page_paths) >= 3 and os.path.exists(page_paths[2]):
            img = Image.open(page_paths[2])
            orig_info = dict(img.info)   # 원본 메타데이터(DPI 등) 캡처
            w, h = img.size
            sx, sy = w / 595.0, h / 842.0

            draw = ImageDraw.Draw(img)
            # 민감정보 조회, 개인(신용)정보 및 공공정보 조회 동의
            p3_coords = [(515, 370), (515, 475)]
            for cx, cy in p3_coords:
                _draw_checkmark(draw, cx * sx, cy * sy, sx, sy)

            # 서명(인) 란 서명 드로잉
            _draw_signature(draw, 250 * sx, 635 * sy, sx, sy)
            os.makedirs(os.path.dirname(output_paths[2]), exist_ok=True)
            _save_png_preserving(img, output_paths[2], orig_info, logger)
            saved_count += 1
            logger.info(f"PNG 3페이지 동의함 체크 및 필기체 서명 드로잉 완료: {os.path.basename(output_paths[2])}")
        elif len(page_paths) >= 3:
            logger.warning(f"PNG 3페이지 입력 파일이 없어 건너뜁니다: {page_paths[2]}")

        if saved_count == 0:
            logger.error("스탬핑할 PNG 입력 파일을 하나도 찾지 못해 결과물이 저장되지 않았습니다.")
            return False
        return True
    except Exception as e:
        logger.error(f"동의서 PNG 스탬핑 작업 중 예외 발생: {e}")
        return False

def _draw_checkmark(draw, cx, cy, sx, sy):
    """지정한 중심 좌표 (cx, cy)에 V자 체크 표시를 그립니다."""
    ox = random.uniform(-0.8, 0.8) * sx
    oy = random.uniform(-0.8, 0.8) * sy
    
    # 짙은 네이비/블랙 색상
    color = (13, 13, 64)
    
    # 스케일에 비례하는 획 굵기 설정 (기본 2px)
    width = max(2, int(2 * (sx + sy) / 2))
    
    # 3개 지점 획 설계
    p1 = (cx - 5.5 * sx + ox, cy - 1.0 * sy + oy)
    p2 = (cx - 1.5 * sx + ox, cy + 4.5 * sy + oy)
    p3 = (cx + 6.0 * sx + ox, cy - 5.5 * sy + oy)
    
    draw.line([p1, p2, p3], fill=color, width=width, joint="round")

def _draw_signature(draw, cx, cy, sx, sy):
    """서명란 중앙 (cx, cy) 영역에 필기체 서명 곡선을 그립니다."""
    color = (13, 13, 38)
    points = []
    
    width = max(2, int(2.2 * (sx + sy) / 2))
    
    # 1) 기본 필기 곡선 생성
    for x_off in range(-25, 26, 4):
        y_off = (x_off / 9.0) * (x_off / 9.0 - 1.2) + random.uniform(-1.2, 1.2)
        points.append((cx + x_off * sx, cy + y_off * sy))
        
    # 2) 루프/꼬리 추가
    points.append((cx + 28 * sx, cy + (-4 + random.uniform(-1, 1)) * sy))
    points.append((cx + 22 * sx, cy + (-8 + random.uniform(-1, 1)) * sy))
    points.append((cx + 26 * sx, cy + (-2 + random.uniform(-1, 1)) * sy))
    points.append((cx + 34 * sx, cy + (1 + random.uniform(-1, 1)) * sy))
    
    for i in range(len(points) - 1):
        draw.line([points[i], points[i+1]], fill=color, width=width, joint="round")
