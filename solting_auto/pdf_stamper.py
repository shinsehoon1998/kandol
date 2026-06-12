import os
import random
import fitz  # PyMuPDF

def split_pdf(input_path: str, output_paths: list, logger) -> bool:
    """다중 입력으로 생성된 통합 PDF 동의서를 각 고객별 3페이지씩 분할하여 개별 파일로 저장합니다."""
    try:
        if not os.path.exists(input_path):
            logger.error(f"분할할 원본 통합 PDF 파일이 존재하지 않습니다: {input_path}")
            return False

        doc = fitz.open(input_path)
        num_pages = len(doc)
        
        for idx, out_path in enumerate(output_paths):
            start_page = idx * 3
            end_page = start_page + 3
            
            if start_page >= num_pages:
                logger.warning(f"페이지 범위 초과로 분할을 조기 종료합니다. (현재 페이지: {start_page}, 총 페이지: {num_pages})")
                break
                
            new_doc = fitz.open()
            new_doc.insert_pdf(doc, from_page=start_page, to_page=min(end_page - 1, num_pages - 1))
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            new_doc.save(out_path)
            new_doc.close()
            logger.info(f"개별 PDF 분할 완료: {os.path.basename(out_path)}")
            
        doc.close()
        return True
    except Exception as e:
        logger.error(f"PDF 분할 중 예외 발생: {e}")
        return False

def stamp_single_pdf(input_path: str, output_path: str, logger) -> bool:
    """다운로드받은 동의서 PDF 파일에 동의함 체크표시 및 서명을 주입하여 다른 폴더에 저장합니다."""
    try:
        if not os.path.exists(input_path):
            logger.error(f"동의서 원본 파일이 존재하지 않습니다: {input_path}")
            return False

        # 출력 폴더 생성
        os.makedirs(os.path.dirname(output_path), exist_ok=True)

        doc = fitz.open(input_path)
        logger.info(f"동의서 PDF 파일 오픈 성공: {os.path.basename(input_path)} (총 {len(doc)}페이지)")

        # 1페이지 처리
        if len(doc) >= 1:
            page = doc[0]
            # 고유식별정보, 민감정보, 개인(신용)정보 수집/이용 동의
            p1_coords = [(515, 315), (515, 355), (515, 490)]
            for cx, cy in p1_coords:
                _draw_checkmark(page, cx, cy)
            logger.info("1페이지 동의함 체크 완료")

        # 2페이지 처리
        if len(doc) >= 2:
            page = doc[1]
            # 고유식별정보, 민감정보, 개인(신용)정보 제공 동의, 국외 제공 동의
            p2_coords = [(515, 215), (515, 265), (515, 375), (490, 720)]
            for cx, cy in p2_coords:
                _draw_checkmark(page, cx, cy)
            logger.info("2페이지 동의함 체크 완료")

        # 3페이지 처리
        if len(doc) >= 3:
            page = doc[2]
            # 민감정보 조회, 개인(신용)정보 및 공공정보 조회 동의
            p3_coords = [(515, 370), (515, 475)]
            for cx, cy in p3_coords:
                _draw_checkmark(page, cx, cy)
            
            # 서명(인) 란 서명 드로잉
            _draw_signature(page, 250, 635)
            logger.info("3페이지 동의함 체크 및 필기체 서명 드로잉 완료")

        # 저장
        doc.save(output_path)
        doc.close()
        logger.info(f"동의서 스탬핑 완료 및 저장 성공: {output_path}")
        return True
    except Exception as e:
        logger.error(f"동의서 PDF 스탬핑 작업 중 예외 발생: {e}")
        return False


def _draw_checkmark(page, cx, cy):
    """지정한 중심 좌표 (cx, cy)에 약간의 무작위성을 부여하여 자연스러운 V자 체크 표시를 그립니다."""
    ox = random.uniform(-0.8, 0.8)
    oy = random.uniform(-0.8, 0.8)
    
    # 볼펜 느낌의 짙은 네이비/블랙 색상 (R, G, B)
    color = (0.05, 0.05, 0.25)
    
    # 체크 표시의 3개 지점 획 설계
    p1 = fitz.Point(cx - 5.5 + ox, cy - 1.0 + oy)
    p2 = fitz.Point(cx - 1.5 + ox, cy + 4.5 + oy)
    p3 = fitz.Point(cx + 6.0 + ox, cy - 5.5 + oy)
    
    page.draw_polyline([p1, p2, p3], color=color, width=1.8, lineCap=1)


def _draw_signature(page, cx, cy):
    """서명란 중앙 (cx, cy) 영역에 사람이 직접 수필 서명한 듯한 꼬리가 있는 필기체 스타일 곡선을 그립니다."""
    color = (0.05, 0.05, 0.15)
    points = []
    
    # 1) 왼쪽에서 오른쪽으로 가는 기본 필기 곡선 생성
    for x_off in range(-25, 26, 4):
        # 파도 모양 굴곡 함수 + 무작위 편차
        y_off = (x_off / 9.0) * (x_off / 9.0 - 1.2) + random.uniform(-1.2, 1.2)
        points.append(fitz.Point(cx + x_off, cy + y_off))
        
    # 2) 수필 느낌의 마지막 루프/꼬리 스트로크 추가
    points.append(fitz.Point(cx + 28, cy - 4 + random.uniform(-1, 1)))
    points.append(fitz.Point(cx + 22, cy - 8 + random.uniform(-1, 1)))
    points.append(fitz.Point(cx + 26, cy - 2 + random.uniform(-1, 1)))
    points.append(fitz.Point(cx + 34, cy + 1 + random.uniform(-1, 1)))
    
    page.draw_polyline(points, color=color, width=2.2, lineCap=1)
