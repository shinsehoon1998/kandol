"""처리 오케스트레이션. 엑셀 1건(파일) 다단계 처리 파이프라인.

흐름(PRD 5.1 + 2단계 확장):
  엑셀 읽기 -> (행별) 검증 -> [단계별: 중복체크 -> 등록] -> 결과기록 -> 리포트

단계:
  - solting   : 1단계 솔팅프로그램 전산등록
  - insurance : 2단계 보험사 전산 고객등록 + 동의서 PDF 다운로드

config["stages"] 로 단계 on/off. 단계별로 독립된 전화번호 중복 저장소를 사용한다.
dry_run=True 이면 브라우저 자동화를 생략하고 검증/중복/리포트만 수행한다.
"""

import time
from datetime import datetime
from pathlib import Path

from . import excel_reader, validators
from .dedup import PhoneDedup
from .reporter import (ReportSummary, RowResult, SUCCESS, FAIL, SKIP, write_report,
                       REASON_DUP_LOCAL)

# 로컬 기등록 스킵 사유(출처 명시)
_LOCAL_DUP_REASON = f"{REASON_DUP_LOCAL}: 이 PC에서 과거 성공 등록한 고객"

STAGE_SOLTING = "솔팅"
STAGE_INSURANCE = "보험사"


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _enabled_stages(config: dict) -> list:
    st = config.get("stages", {"solting": True})
    stages = []
    if st.get("solting", True):
        stages.append(STAGE_SOLTING)
    if st.get("insurance", False):
        stages.append(STAGE_INSURANCE)
    return stages


def process_file(xlsx_path: str, config: dict, logger, dry_run: bool = False,
                 progress_cb=None, stop_check_cb=None) -> ReportSummary:
    """progress_cb(done, total, last_result) - 행 처리마다 호출(웹 진행률용, 선택).
    stop_check_cb() - 중단 요청 여부를 반환하는 콜백 함수(선택).
    """
    run = config["run"]
    columns = config["columns"]
    fmt = config.get("format", {})
    out_folder = run.get("output_folder", "./output")
    Path(out_folder).mkdir(parents=True, exist_ok=True)
    shot_folder = Path(out_folder) / "screenshots"
    use_checksum = fmt.get("jumin_checksum", False)

    stages = _enabled_stages(config)
    if STAGE_SOLTING in stages:
        login_url = config.get("site", {}).get("login_url", "")
        if "example-solting.com" in login_url or "example" in login_url:
            logger.info("[안내] 1단계 솔팅 프로그램 주소가 예시(example-solting.com) 상태이므로 1단계를 자동으로 건너뛰고 2단계만 진행합니다.")
            stages.remove(STAGE_SOLTING)

    summary = ReportSummary()
    source_name = Path(xlsx_path).stem
    started = _now()

    logger.info(f"파일 처리 시작: {Path(xlsx_path).name} (단계={stages}, dry_run={dry_run})")
    records = excel_reader.read_records(xlsx_path, columns)
    logger.info(f"총 {len(records)}행 로드")

    # 단계별 독립 중복 저장소 (기등록 로컬필터 무시 옵션 지원)
    ignore_reg = bool(config.get("insurance", {}).get("ignore_local_dedup", False))
    if ignore_reg:
        logger.info("[중복] 기등록(로컬) 무시 옵션 ON — 과거 등록분도 재처리(파일 내 중복·KB 중복은 유지)")
    dedups = {
        STAGE_SOLTING: PhoneDedup(str(Path(out_folder) / "registered_phones_solting.json"), ignore_registered=ignore_reg),
        STAGE_INSURANCE: PhoneDedup(str(Path(out_folder) / "registered_phones_insurance.json"), ignore_registered=ignore_reg),
    }

    engines = {}
    try:
        if not dry_run:
            engines = _start_engines(stages, config, logger)

        input_mode = config.get("insurance", {}).get("input_mode", "single")
        if input_mode == "batch" and STAGE_INSURANCE in stages:
            logger.info("다중입력(배치) 모드로 동의서 등록 프로세스를 수행합니다.")
            current_chunk = []
            for rec in records:
                if stop_check_cb and stop_check_cb():
                    logger.info("작업 중단 신호가 감지되어 루프 처리를 중지합니다.")
                    raise RuntimeError("사용자 중단 요청")

                # 검증 및 중복 체크 선행
                valid = True
                reason = ""
                for check in (
                    validators.validate_jumin(rec.jumin, use_checksum),
                    validators.validate_name(rec.name),
                    validators.validate_phone(rec.phone),
                ):
                    if not check.ok:
                        valid = False
                        reason = check.reason
                        break

                if not valid:
                    ts = _now()
                    r = RowResult(
                        row_no=rec.row_no, jumin=rec.jumin, name=rec.name, phone=rec.phone,
                        status=SKIP, timestamp=ts, reason=reason
                    )
                    for st in stages:
                        _set_stage(r, st, SKIP, reason)
                    summary.add(r)
                    if progress_cb:
                        progress_cb(summary.total, len(records), r)
                    continue

                is_dup = False
                for st in stages:
                    dedup = dedups[st]
                    if dedup.is_duplicate(rec.phone):
                        is_dup = True
                        reason = _LOCAL_DUP_REASON
                        dedup.mark_seen(rec.phone)
                        break

                if is_dup:
                    ts = _now()
                    r = RowResult(
                        row_no=rec.row_no, jumin=rec.jumin, name=rec.name, phone=rec.phone,
                        status=SKIP, timestamp=ts, reason=reason
                    )
                    for st in stages:
                        _set_stage(r, st, SKIP, reason)
                    summary.add(r)
                    if progress_cb:
                        progress_cb(summary.total, len(records), r)
                    continue

                for st in stages:
                    dedups[st].mark_seen(rec.phone)

                current_chunk.append(rec)

                if len(current_chunk) == 10:
                    _process_chunk(current_chunk, stages, engines, dedups, run, shot_folder, logger, dry_run, config, summary, progress_cb, len(records))
                    current_chunk = []
                    if not dry_run:
                        delay = run.get("row_delay_sec", 1.0)
                        slept = 0.0
                        while slept < delay:
                            import solting_auto
                            solting_auto.check_stop()
                            time.sleep(0.1)
                            slept += 0.1

            if current_chunk:
                _process_chunk(current_chunk, stages, engines, dedups, run, shot_folder, logger, dry_run, config, summary, progress_cb, len(records))

        else:
            # ── 50명 단위 자동 폴더링 설정 (KB 스캔 렉 완화용) ──
            ins_cfg = config.get("insurance", {})
            auto_folder_enabled = bool(ins_cfg.get("auto_folder_enabled", False))
            try:
                folder_interval = int(ins_cfg.get("auto_folder_interval", 50))
            except (TypeError, ValueError):
                folder_interval = 50
            if folder_interval <= 0:
                if auto_folder_enabled:
                    logger.warning(f"폴더링 분할 인원이 비정상({folder_interval}) → 폴더링 비활성 처리")
                auto_folder_enabled = False
            run_date = datetime.now().strftime("%Y%m%d")
            ins_engine = engines.get(STAGE_INSURANCE)
            pdf_base = Path(ins_cfg.get("pdf_folder", "./output/consent_pdfs"))
            stamped_base = Path(ins_cfg.get("pdf_stamped_folder", "./output/consent_pdfs_stamped"))
            success_count = 0
            if auto_folder_enabled:
                logger.info(f"[폴더링] 성공 {folder_interval}명 단위로 '날짜_조번호' 하위폴더 자동 분산 (날짜={run_date})")

            # 연속 실패 자동중단(서킷브레이커): 세션 만료/브라우저 문제로 이후 전부 실패하는
            # 상황에서 수천 건을 헛되이 재시도(수 시간 낭비)하지 않도록 조기 중단.
            consec_fail = 0
            try:
                max_consec = int(run.get("max_consecutive_fails", 20))
            except (TypeError, ValueError):
                max_consec = 20
            # 연속 실패 시 자동 재로그인(세션 리셋) 임계 — 0이면 비활성(옵트인)
            try:
                relogin_threshold = int(config.get("insurance", {}).get("relogin_fail_threshold", 0))
            except (TypeError, ValueError):
                relogin_threshold = 0

            for rec in records:
                if stop_check_cb and stop_check_cb():
                    logger.info("작업 중단 신호가 감지되어 루프 처리를 중지합니다.")
                    raise RuntimeError("사용자 중단 요청")

                # 현재 고객을 저장할 조 하위폴더를 처리 전에 지정 (원본=engine.pdf_dir, 스탬프=override)
                stamped_override = None
                if auto_folder_enabled:
                    jo = success_count // folder_interval + 1
                    sub = f"{run_date}_{jo}"
                    if ins_engine is not None:
                        ins_engine.pdf_dir = pdf_base / sub
                    stamped_override = str(stamped_base / sub)

                res = _process_row(rec, stages, engines, dedups, run, use_checksum,
                                   shot_folder, logger, dry_run,
                                   stamped_dir_override=stamped_override)
                if auto_folder_enabled and res.status == SUCCESS:
                    success_count += 1
                summary.add(res)
                if progress_cb:
                    progress_cb(summary.total, len(records), res)

                # 연속 실패 서킷브레이커 — 연속 N건 실패면 세션/브라우저 문제로 보고 자동 중단
                if res.status == FAIL:
                    consec_fail += 1
                    # 재로그인 임계 도달 시(서킷브레이커 이전에) 세션 리셋 1회 시도 → 회복되면 계속
                    if (relogin_threshold > 0 and consec_fail == relogin_threshold
                            and ins_engine is not None and STAGE_INSURANCE in stages):
                        try:
                            logger.info(f"연속 {consec_fail}건 실패 → 세션 리셋 위해 자동 재로그인 시도")
                            ins_engine.login(force=True)
                            logger.info("자동 재로그인 성공 — 연속실패 카운트 초기화 후 계속 진행")
                            consec_fail = 0
                        except Exception as relog_err:
                            logger.warning(f"자동 재로그인 실패(계속 진행, 서킷브레이커까지 대기): {relog_err}")
                    if max_consec > 0 and consec_fail >= max_consec:
                        logger.error(f"연속 {consec_fail}건 실패 감지 → 세션 만료/브라우저 문제로 추정하여 자동 중단합니다.")
                        raise RuntimeError(
                            f"연속 {consec_fail}건 실패로 자동 중단했습니다. KB 로그인 세션·브라우저 상태를 확인하고 재로그인 후 다시 시도해 주세요. "
                            f"(이미 처리된 건은 기등록으로 건너뜁니다)")
                else:
                    consec_fail = 0

                # [수정] 4단계 (KB스캔) 전송 실패 시, 다음 작업을 즉시 일시정지(중단)합니다.
                if res.kb_scan_status == FAIL:
                    logger.error(f"[{rec.row_no}행] 4단계 (KB스캔) 전송 실패로 인해 전체 작업을 일시정지(중단)합니다.")
                    raise RuntimeError("4단계 (KB스캔) 전송 실패로 인해 작업을 일시정지(중단)합니다. 화면 및 로그를 확인한 후 다시 시도해 주세요.")
                    
                if not dry_run:
                    delay = run.get("row_delay_sec", 1.0)
                    slept = 0.0
                    while slept < delay:
                        import solting_auto
                        solting_auto.check_stop()
                        time.sleep(0.1)
                        slept += 0.1

        for d in dedups.values():
            d.save()
            
    except Exception as e:
        # 중도 오류/중단 시에도 현재까지 완료된 결과를 엑셀 리포트 및 summary 속성에 동기화합니다.
        e.summary = summary
        try:
            report_path = write_report(summary, out_folder, source_name, started)
            logger.info(f"중도 중단 리포트 저장 완료: {report_path}")
        except Exception as report_err:
            logger.error(f"중도 중단 리포트 저장 실패: {report_err}")
        raise e
        
    finally:
        for eng in engines.values():
            try:
                eng.__exit__(None, None, None)
            except Exception:
                pass

    report_path = write_report(summary, out_folder, source_name, started)
    logger.info(f"리포트 저장: {report_path}")
    logger.info(f"처리 완료 - {summary.as_text()}")
    return summary


def _start_engines(stages, config, logger):
    """활성 단계의 브라우저 엔진을 띄우고 로그인."""
    engines = {}
    if STAGE_SOLTING in stages:
        from .automation import SoltingAutomation
        eng = SoltingAutomation(config, logger).__enter__()
        eng.login()
        engines[STAGE_SOLTING] = eng
    if STAGE_INSURANCE in stages:
        from .insurance import InsuranceAutomation
        eng = InsuranceAutomation(config, logger).__enter__()
        eng.login()
        engines[STAGE_INSURANCE] = eng
    return engines


def _process_row(rec, stages, engines, dedups, run, use_checksum, shot_folder, logger, dry_run,
                 stamped_dir_override=None):
    """행 1건을 모든 활성 단계에 대해 처리 -> RowResult.
    stamped_dir_override: 지정 시 스탬프 결과를 이 폴더에 저장(50명 단위 폴더링용)."""
    ts = _now()
    r = RowResult(
        row_no=rec.row_no, jumin=rec.jumin, name=rec.name, phone=rec.phone,
        status=SKIP, timestamp=ts,
    )

    # 1) 검증 (FR-4.1) - 실패 시 모든 단계 건너뜀
    for check in (
        validators.validate_jumin(rec.jumin, use_checksum),
        validators.validate_name(rec.name),
        validators.validate_phone(rec.phone),
    ):
        if not check.ok:
            logger.info(f"[{rec.row_no}행] Skip - {check.reason}")
            r.status = SKIP
            r.reason = check.reason
            for st in stages:
                _set_stage(r, st, SKIP, "검증 실패")
            return r

    # 2) 단계별 처리
    for st in stages:
        status, reason, pdf = _run_stage(
            st, rec, engines.get(st), dedups[st], run, shot_folder, logger, dry_run
        )
        _set_stage(r, st, status, reason, pdf)

        # 동의서 자동 서명/스탬핑 처리 연동
        if st == STAGE_INSURANCE and status == SUCCESS and pdf:
            engine = engines.get(st)
            if engine:
                ins_cfg = engine.ins
                stamping_enabled = ins_cfg.get("stamping_enabled", True)
                if stamping_enabled:
                    stamped_folder = stamped_dir_override or ins_cfg.get("pdf_stamped_folder", "./output/consent_pdfs_stamped")
                    from pathlib import Path
                    file_path = Path(pdf)
                    
                    logger.info(f"[{rec.row_no}행] 동의서 자동 서명 및 스탬핑 시작")
                    if file_path.suffix.lower() == ".png":
                        # 실제 저장된 페이지(_1, _2, ...)를 동적으로 수집 (하드코딩 3페이지 제거)
                        page_paths, output_paths = [], []
                        for i in range(1, 20):
                            src = file_path.parent / f"{file_path.stem}_{i}.png"
                            if not src.exists():
                                break
                            page_paths.append(str(src))
                            output_paths.append(str(Path(stamped_folder) / src.name))
                        if not page_paths:
                            logger.warning(f"[{rec.row_no}행] 스탬핑할 PNG 페이지를 찾지 못했습니다: {file_path.parent / file_path.stem}_N.png")
                        from . import png_stamper
                        success = png_stamper.stamp_single_png_set(page_paths, output_paths, logger)
                        if success:
                            # 실제 저장된 첫 페이지 경로를 기록 (존재하지 않는 합본명 대신)
                            r.consent_stamped_pdf = output_paths[0]
                    else:
                        stamped_path = Path(stamped_folder) / file_path.name
                        from . import pdf_stamper
                        success = pdf_stamper.stamp_single_pdf(str(file_path), str(stamped_path), logger)
                        if success:
                            r.consent_stamped_pdf = str(stamped_path)
                else:
                    r.consent_stamped_pdf = pdf

                # KB스캔 자동 업로드 연동 (Single 모드)
                kb_scan_enabled = ins_cfg.get("kb_scan_enabled", False)
                if kb_scan_enabled and r.consent_stamped_pdf:
                    logger.info(f"[{rec.row_no}행] 동의서 KB스캔 자동 업로드 시작")
                    try:
                        scan_success = engine.upload_to_kb_scan(r.consent_stamped_pdf)
                        if scan_success:
                            r.kb_scan_status = SUCCESS
                            logger.info(f"[{rec.row_no}행] 동의서 KB스캔 자동 업로드 성공")
                        else:
                            r.kb_scan_status = FAIL
                            r.kb_scan_reason = "업로드 실패"
                            logger.error(f"[{rec.row_no}행] 동의서 KB스캔 자동 업로드 실패")
                    except Exception as scan_err:
                        r.kb_scan_status = FAIL
                        r.kb_scan_reason = str(scan_err)
                        logger.error(f"[{rec.row_no}행] 동의서 KB스캔 중 오류 발생: {scan_err}")

    # 3) 전체 상태 산정
    stage_statuses = [_get_stage_status(r, st) for st in stages]
    if FAIL in stage_statuses:
        r.status = FAIL
    elif SUCCESS in stage_statuses:
        r.status = SUCCESS
    else:
        r.status = SKIP
        # 실제 단계 사유(로컬 기등록 / KB 기등록 / 검증 등)를 보존 — 하드코딩 덮어쓰기 금지
        r.reason = next((rs for rs in (_get_stage_reason(r, st) for st in stages) if rs), _LOCAL_DUP_REASON)
    return r


def _run_stage(stage, rec, engine, dedup, run, shot_folder, logger, dry_run):
    """단일 단계 처리 -> (status, reason, pdf_path)."""
    # 중복 체크 (FR-4.2) - 단계별 전화번호 기준(로컬 기등록/파일 내 중복)
    if dedup.is_duplicate(rec.phone):
        dedup.mark_seen(rec.phone)
        logger.info(f"[{rec.row_no}행] {stage} Skip - {_LOCAL_DUP_REASON}")
        return SKIP, _LOCAL_DUP_REASON, ""
    dedup.mark_seen(rec.phone)

    if dry_run:
        logger.info(f"[{rec.row_no}행] {stage} (dry-run) 등록 대상 OK")
        return SUCCESS, "(dry-run)", ""

    from .automation import RegisterError, RetryableError, DuplicateCustomerError
    retry_count = run.get("retry_count", 2)
    retry_delay = run.get("retry_delay_sec", 3)

    for attempt in range(retry_count + 1):
        try:
            pdf = ""
            if stage == STAGE_SOLTING:
                engine.register_one(rec.jumin, rec.name, rec.phone)
            else:  # 보험사: 등록 + 동의서 PDF
                pdf = engine.register_and_consent(rec.jumin, rec.name, rec.phone)
            dedup.mark_registered(rec.phone)
            logger.info(f"[{rec.row_no}행] {stage} 등록 성공")
            return SUCCESS, "", pdf
        except DuplicateCustomerError as e:
            logger.info(f"[{rec.row_no}행] {stage} 건너뜀 - {e}")
            return SKIP, str(e), ""
        except RetryableError as e:
            if attempt < retry_count:
                # KB 서버 통신오류(-S0001 등)는 서버 회복 시간을 위해 더 길게 쿨다운 후 재시도
                cool = retry_delay
                if any(k in str(e) for k in ("서버", "통신", "S0001")):
                    try:
                        cool = max(retry_delay, int(run.get("server_error_cooldown_sec", 60)))
                    except (TypeError, ValueError):
                        cool = max(retry_delay, 60)
                    logger.info(f"[{rec.row_no}행] {stage} KB 서버오류 감지 → {cool}초 쿨다운 후 재시도 {attempt+1}/{retry_count}")
                else:
                    logger.info(f"[{rec.row_no}행] {stage} 재시도 {attempt+1}/{retry_count} - {e}")
                time.sleep(cool)
                continue
            _shot(engine, shot_folder, rec.row_no, stage)
            return FAIL, f"재시도 초과: {e}", ""
        except RegisterError as e:
            _shot(engine, shot_folder, rec.row_no, stage)
            logger.info(f"[{rec.row_no}행] {stage} 실패 - {e}")
            return FAIL, str(e), ""

    return FAIL, "알 수 없는 오류", ""


def _set_stage(r: RowResult, stage, status, reason="", pdf=""):
    if stage == STAGE_SOLTING:
        r.solting_status, r.solting_reason = status, reason
    else:
        r.insurance_status, r.insurance_reason = status, reason
        if pdf:
            r.consent_pdf = pdf


def _get_stage_status(r: RowResult, stage):
    return r.solting_status if stage == STAGE_SOLTING else r.insurance_status


def _get_stage_reason(r: RowResult, stage):
    return r.solting_reason if stage == STAGE_SOLTING else r.insurance_reason


def _shot(engine, shot_folder, row_no, stage):
    if engine is None or not hasattr(engine, "screenshot"):
        return ""
    Path(shot_folder).mkdir(parents=True, exist_ok=True)
    path = str(Path(shot_folder) / f"row_{row_no}_{stage}.png")
    try:
        return engine.screenshot(path)
    except Exception:
        return ""

def _process_chunk(chunk, stages, engines, dedups, run, shot_folder, logger, dry_run, config, summary, progress_cb, total_records):
    import solting_auto
    solting_auto.check_stop()
    
    # 결과를 담을 딕셔너리 초기화
    chunk_results = {}
    for rec in chunk:
        ts = _now()
        chunk_results[rec.row_no] = RowResult(
            row_no=rec.row_no, jumin=rec.jumin, name=rec.name, phone=rec.phone,
            status=SKIP, timestamp=ts
        )
        
    # 1) 1단계 솔팅 프로그램 개별 실행
    if STAGE_SOLTING in stages and not dry_run:
        for rec in chunk:
            solting_auto.check_stop()
            r = chunk_results[rec.row_no]
            solting_status, solting_reason, _ = _run_stage(
                STAGE_SOLTING, rec, engines.get(STAGE_SOLTING),
                dedups[STAGE_SOLTING], run, shot_folder, logger, dry_run
            )
            _set_stage(r, STAGE_SOLTING, solting_status, solting_reason)
            
    # 2) 2단계 보험사 일괄 실행
    if STAGE_INSURANCE in stages:
        if dry_run:
            for rec in chunk:
                r = chunk_results[rec.row_no]
                _set_stage(r, STAGE_INSURANCE, SUCCESS, "(dry-run)")
        else:
            batch_data = []
            for rec in chunk:
                batch_data.append({
                    'jumin': rec.jumin,
                    'name': rec.name,
                    'phone': rec.phone,
                    'row_no': rec.row_no
                })
                
            file_format = config.get("insurance", {}).get("oz", {}).get("file_format", "PDF")
            ins_cfg = config["insurance"]  # for loop 전에 정의 (NameError 방지)
            batch_results = engines[STAGE_INSURANCE].register_and_consent_batch(batch_data, file_format)

            # 결과 맵핑 및 스탬핑
            for b_res in batch_results:
                rec_row = b_res["row_no"]
                r = chunk_results[rec_row]

                _set_stage(r, STAGE_INSURANCE, b_res["status"], b_res.get("reason", ""))
                if b_res["status"] == SUCCESS:
                    pdf_path_str = b_res["pdf_path"]
                    r.consent_pdf = pdf_path_str

                    # 스탬핑 연동
                    stamping_enabled = ins_cfg.get("stamping_enabled", True)
                    if stamping_enabled and pdf_path_str:
                        stamped_folder = ins_cfg.get("pdf_stamped_folder", "./output/consent_pdfs_stamped")
                        from pathlib import Path
                        file_path = Path(pdf_path_str)

                        logger.info(f"[{rec_row}행] 동의서 자동 서명 및 스탬핑 시작")
                        if file_path.suffix.lower() == ".png":
                            # 실제 저장된 페이지(_1, _2, ...)를 동적으로 수집 (하드코딩 3페이지 제거)
                            page_paths, output_paths = [], []
                            for i in range(1, 20):
                                src = file_path.parent / f"{file_path.stem}_{i}.png"
                                if not src.exists():
                                    break
                                page_paths.append(str(src))
                                output_paths.append(str(Path(stamped_folder) / src.name))
                            if not page_paths:
                                logger.warning(f"[{rec_row}행] 스탬핑할 PNG 페이지를 찾지 못했습니다: {file_path.parent / file_path.stem}_N.png")
                            from . import png_stamper
                            success = png_stamper.stamp_single_png_set(page_paths, output_paths, logger)
                            if success:
                                # 실제 저장된 첫 페이지 경로를 기록 (존재하지 않는 합본명 대신)
                                r.consent_stamped_pdf = output_paths[0]
                        else:
                            stamped_path = Path(stamped_folder) / file_path.name
                            from . import pdf_stamper
                            success = pdf_stamper.stamp_single_pdf(str(file_path), str(stamped_path), logger)
                            if success:
                                r.consent_stamped_pdf = str(stamped_path)
                    else:
                        r.consent_stamped_pdf = pdf_path_str

                    # 등록 성공 마크
                    dedups[STAGE_INSURANCE].mark_registered(r.phone)

            # KB스캔 자동 업로드 연동 (Batch 모드)
            kb_scan_enabled = ins_cfg.get("kb_scan_enabled", False)
            if kb_scan_enabled:
                stamped_paths = []
                row_map = {}
                for rec in chunk:
                    r = chunk_results[rec.row_no]
                    if r.insurance_status == SUCCESS and r.consent_stamped_pdf:
                        stamped_paths.append(r.consent_stamped_pdf)
                        row_map[r.consent_stamped_pdf] = rec.row_no
                
                if stamped_paths:
                    logger.info(f"배치 완료 고객 {len(stamped_paths)}명에 대해 KB스캔 일괄 업로드 시작...")
                    try:
                        engine = engines.get(STAGE_INSURANCE)
                        if engine:
                            scan_success = engine.upload_to_kb_scan(stamped_paths)
                            if scan_success:
                                logger.info("배치 KB스캔 일괄 업로드 성공")
                                for path in stamped_paths:
                                    row_no = row_map[path]
                                    chunk_results[row_no].kb_scan_status = SUCCESS
                            else:
                                logger.error("배치 KB스캔 일괄 업로드 실패")
                                for path in stamped_paths:
                                    row_no = row_map[path]
                                    chunk_results[row_no].kb_scan_status = FAIL
                                    chunk_results[row_no].kb_scan_reason = "일괄 업로드 실패"
                        else:
                            logger.error("보험사 엔진이 없어 KB스캔을 진행할 수 없습니다.")
                    except Exception as scan_err:
                        logger.error(f"배치 KB스캔 일괄 업로드 중 오류 발생: {scan_err}")
                        for path in stamped_paths:
                            row_no = row_map[path]
                            chunk_results[row_no].kb_scan_status = FAIL
                            chunk_results[row_no].kb_scan_reason = str(scan_err)
                    
    # 3) 전체 상태 산정 및 누적
    for rec in chunk:
        r = chunk_results[rec.row_no]
        stage_statuses = [_get_stage_status(r, st) for st in stages]
        if FAIL in stage_statuses:
            r.status = FAIL
        elif SUCCESS in stage_statuses:
            r.status = SUCCESS
        else:
            r.status = SKIP
            
        summary.add(r)
        if progress_cb:
            progress_cb(summary.total, total_records, r)
