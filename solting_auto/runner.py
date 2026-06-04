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
from .reporter import ReportSummary, RowResult, SUCCESS, FAIL, SKIP, write_report

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
                 progress_cb=None) -> ReportSummary:
    """progress_cb(done, total, last_result) - 행 처리마다 호출(웹 진행률용, 선택)."""
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

    # 단계별 독립 중복 저장소
    dedups = {
        STAGE_SOLTING: PhoneDedup(str(Path(out_folder) / "registered_phones_solting.json")),
        STAGE_INSURANCE: PhoneDedup(str(Path(out_folder) / "registered_phones_insurance.json")),
    }

    engines = {}
    try:
        if not dry_run:
            engines = _start_engines(stages, config, logger)

        for rec in records:
            res = _process_row(rec, stages, engines, dedups, run, use_checksum,
                               shot_folder, logger, dry_run)
            summary.add(res)
            if progress_cb:
                progress_cb(summary.total, len(records), res)
                
            # [수정] 4단계 (KB스캔) 전송 실패 시, 다음 작업을 즉시 일시정지(중단)합니다.
            if res.kb_scan_status == FAIL:
                logger.error(f"[{rec.row_no}행] 4단계 (KB스캔) 전송 실패로 인해 전체 작업을 일시정지(중단)합니다.")
                raise RuntimeError("4단계 (KB스캔) 전송 실패로 인해 작업을 일시정지(중단)합니다. 화면 및 로그를 확인한 후 다시 시도해 주세요.")
                
            if not dry_run:
                time.sleep(run.get("row_delay_sec", 1.0))

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


def _process_row(rec, stages, engines, dedups, run, use_checksum, shot_folder, logger, dry_run):
    """행 1건을 모든 활성 단계에 대해 처리 -> RowResult."""
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
                    stamped_folder = ins_cfg.get("pdf_stamped_folder", "./output/consent_pdfs_stamped")
                    from pathlib import Path
                    pdf_path = Path(pdf)
                    stamped_path = Path(stamped_folder) / pdf_path.name
                    
                    logger.info(f"[{rec.row_no}행] 동의서 자동 서명 및 스탬핑 시작")
                    from . import pdf_stamper
                    success = pdf_stamper.stamp_single_pdf(str(pdf_path), str(stamped_path), logger)
                    if success:
                        r.consent_stamped_pdf = str(stamped_path)

    # 3) 전체 상태 산정
    stage_statuses = [_get_stage_status(r, st) for st in stages]
    if FAIL in stage_statuses:
        r.status = FAIL
    elif SUCCESS in stage_statuses:
        r.status = SUCCESS
    else:
        r.status = SKIP
        r.reason = "전화번호 중복"
    return r


def _run_stage(stage, rec, engine, dedup, run, shot_folder, logger, dry_run):
    """단일 단계 처리 -> (status, reason, pdf_path)."""
    # 중복 체크 (FR-4.2) - 단계별 전화번호 기준
    if dedup.is_duplicate(rec.phone):
        dedup.mark_seen(rec.phone)
        logger.info(f"[{rec.row_no}행] {stage} Skip - 전화번호 중복")
        return SKIP, "전화번호 중복", ""
    dedup.mark_seen(rec.phone)

    if dry_run:
        logger.info(f"[{rec.row_no}행] {stage} (dry-run) 등록 대상 OK")
        return SUCCESS, "(dry-run)", ""

    from .automation import RegisterError, RetryableError
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
        except RetryableError as e:
            if attempt < retry_count:
                logger.info(f"[{rec.row_no}행] {stage} 재시도 {attempt+1}/{retry_count} - {e}")
                time.sleep(retry_delay)
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


def _shot(engine, shot_folder, row_no, stage):
    if engine is None or not hasattr(engine, "screenshot"):
        return ""
    Path(shot_folder).mkdir(parents=True, exist_ok=True)
    path = str(Path(shot_folder) / f"row_{row_no}_{stage}.png")
    try:
        return engine.screenshot(path)
    except Exception:
        return ""
