# 보픽 리드 인입 API — 깐돌이(kandori) 연동 스펙

> 대상: 깐돌이 개발팀
> 목적: 깐돌이 고객 DB → 보픽 리드로 전달(인입). 인입된 리드는 보픽 상담사 인박스에 즉시 노출됨.
> 엔드포인트 상태: **ACTIVE** (Edge Function `ingest-kandori`)

---

## 1. 엔드포인트
```
POST https://aftallfjjwzfphqeuwuc.supabase.co/functions/v1/ingest-kandori
```

## 2. 인증 (헤더)
```
x-api-key: ${BOPICK_API_KEY}  # ← Vercel 환경변수, 저장소에 커밋 금지
Content-Type: application/json
```
- 채널: **깐돌이DB** · source 태그: `grandfather`
- ⚠️ 이 키는 **리드 적재 권한**이 있으니 **서버 사이드에서만** 사용(브라우저/클라이언트 노출 금지)

## 3. 요청 Body
```json
{
  "mode": "upsert",
  "source": "kandori-customer-db",
  "count": 2,
  "customers": [
    {
      "customer_name": "김민수",
      "phone": "010-1234-5678",
      "birth": "1988-03-12",
      "age": 37,
      "gender": "남",
      "monthly_premium": 85000,
      "policy_count": 2,
      "consent_end_date": "2026-12-31",
      "registered_at": "2026-06-01",
      "coverage_detail": { "암": true, "실손": false }
    }
  ]
}
```

### 필드 규칙
| 필드 | 매핑 | 비고 |
|---|---|---|
| `mode` | — | `"skip"`(기본) 또는 `"upsert"`. **[3-1] 참고** |
| `external_id` | 중복 판정 1순위 | **강력 권장.** 없으면 전화번호로만 판정 |
| `customer_name` | 고객명(name) | `phone`과 **둘 중 하나 필수** |
| `phone` | 전화번호 | 중복 판정 2순위 (숫자만 남겨 비교) |
| `birth` | 생년월일 | `YYYY-MM-DD` 문자열 권장 |
| `age`, `gender`, `monthly_premium`, `policy_count`, `consent_end_date`, `registered_at`, `contracts`, 그 외 전체 | 부가정보(metadata) | **모든 컬럼 그대로 보관** (객체·배열도 OK) |

- `consent_end_date`, `registered_at` 도 `YYYY-MM-DD` 문자열 권장
- 위 목록에 없는 컬럼도 전부 보존되므로 자유롭게 추가 가능

### 3-1. `mode` — 중복 고객을 만났을 때

| 값 | 동작 |
|---|---|
| `"skip"` (기본값) | 이미 있는 고객이면 **건너뜀**. 보강된 정보가 반영되지 않음 |
| `"upsert"` | 이미 있는 고객이면 **갱신**. 값이 있는 필드만 덮어쓰고 빈 값은 기존 값 보존 |

- **`mode` 를 생략하면 `skip` 입니다.** 상세수집으로 보강한 데이터를 반영하려면 반드시 `"upsert"` 를 명시하세요.
- 갱신은 **`status`·배정 담당자·방문일정·배차비용을 건드리지 않습니다.** 깐돌이는 그 리드가
  보픽에서 어디까지 진행됐는지 모르기 때문입니다. 상담 중인 리드에 재전송해도 안전합니다.
- 갱신되는 것: `metadata` 전체(병합) · `name` · `phone` · `birth_date`

### 3-2. `external_id` 를 보내주셔야 하는 이유

전화번호가 유일한 매칭 키이면, **번호가 정정되는 순간 같은 사람이 두 건으로 늘어납니다.**
깐돌이 쪽 고객 고유번호를 `external_id`(또는 `customer_id`)로 실어주시면 그걸 1순위 키로 씁니다.
받는 쪽은 이미 구현돼 있으니 필드만 추가하시면 됩니다.

## 4. 응답 (200 OK)
```json
{ "ok": true, "mode": "upsert", "source": "kandori-customer-db",
  "received": 3, "inserted": 0, "updated": 3, "skipped": 0, "errors": 0 }
```
| 필드 | 의미 |
|---|---|
| `mode` | 실제 적용된 모드 (생략 시 `"skip"`) |
| `received` | 수신한 customers 수 |
| `inserted` | 신규 적재된 리드 수 |
| `updated` | 기존 리드를 갱신한 수 (`mode=upsert` 일 때만 발생) |
| `skipped` | 중복인데 `mode=skip` 이라 건너뜀 + 이름·전화 둘 다 없어 건너뜀 |
| `errors` | 개별 적재/갱신 실패 수 |
| `external_id_lookup_error` | (선택) `external_id` 조회 실패 시에만 포함. 이 경우 전화번호로만 매칭됨 |

## 5. 동작 규칙 (중요)
- 고객 1명 = 보픽 리드 1건, **신규 상태**로 적재 → 상담사에게 즉시 노출
- **멱등(재전송 안전)**: 같은 고객이 이미 있으면 `mode` 에 따라 skip 또는 upsert →
  재시도/재실행해도 중복이 쌓이지 않음
- **같은 배치 안 중복도 방지**: 한 요청에 같은 사람이 두 번 들어와도 1건만 생성
- **동의 만료 자동 플래그**: `consent_end_date`가 지난 고객은 내부적으로 만료 표시(발신 주의용)
- **배치 최대 5,000건 / 요청** — 초과 시 나눠서 전송

## 6. 에러 코드
| HTTP | error | 의미 |
|---|---|---|
| 401 | `missing_api_key` | x-api-key 헤더 없음 |
| 401 | `invalid_api_key` | 키가 유효하지 않거나 비활성 |
| 400 | `customers_required` | customers 배열 없음/형식 오류 |
| 413 | `too_many` | 5,000건 초과 |
| 405 | `method_not_allowed` | POST 외 메서드 |
| 500 | `exception` | 서버 처리 오류(detail 포함) |

## 7. 테스트 (curl)

### 신규 적재
```bash
curl -X POST https://aftallfjjwzfphqeuwuc.supabase.co/functions/v1/ingest-kandori \
  -H "x-api-key: ${BOPICK_API_KEY}  # ← Vercel 환경변수, 저장소에 커밋 금지" \
  -H "content-type: application/json" \
  -d '{
    "source": "kandori-customer-db",
    "count": 1,
    "customers": [
      { "external_id": "KD-000001", "customer_name": "홍길동", "phone": "010-9999-0001",
        "birth": "1990-05-05", "gender": "남", "monthly_premium": 72000, "policy_count": 1,
        "consent_end_date": "2026-12-31", "coverage_detail": { "암": true } }
    ]
  }'
```

### 기존 고객 갱신 (상세수집 반영)
같은 요청에 `"mode": "upsert"` 한 줄만 추가하면 됩니다.
```bash
curl -X POST https://aftallfjjwzfphqeuwuc.supabase.co/functions/v1/ingest-kandori \
  -H "x-api-key: ${BOPICK_API_KEY}  # ← Vercel 환경변수, 저장소에 커밋 금지" \
  -H "content-type: application/json" \
  -d '{
    "mode": "upsert",
    "source": "kandori-customer-db",
    "count": 1,
    "customers": [
      { "external_id": "KD-000001", "customer_name": "홍길동", "phone": "010-9999-0001" }
    ]
  }'
```
→ 응답의 `updated` 가 1이면 정상. 처음에는 **3~5명으로 먼저 확인**한 뒤 전량 전송을 권장합니다.

## 8. 전송 방식
- **실시간**(신규 고객 생길 때마다 1건씩) 또는 **주기 배치**(일괄) 둘 다 동일 엔드포인트 사용
- 실패 시 그대로 재시도 안전(멱등)

## 9. 변경 이력
| 날짜 | 내용 |
|---|---|
| 2026-08-17 | `mode=upsert` 추가(기본값 `skip`), 응답에 `updated` 추가, `external_id` 매칭 지원. Edge Function v8 |

> ⚠️ 2026-08-17 이전 구현은 `mode` 를 보내지 않으므로 **중복 시 전부 skip** 됩니다.
> 상세수집 보강분을 반영하려면 전송 코드에 `"mode": "upsert"` 를 추가해야 합니다.

## 10. 운영 메모 (보픽 측)
- 인입 리드는 `source=grandfather` 태그로 저장되어 채널별 성과 집계(콜센터 콘솔)에 반영됨
- 현재 이 채널 키의 **기본 권역(default_region_id)이 미지정** — 특정 권역 자동배정이 필요하면 보픽 관리자에게 요청
- 서버: Supabase Edge Function `ingest-kandori` (`supabase/functions/ingest-kandori/index.ts`)
