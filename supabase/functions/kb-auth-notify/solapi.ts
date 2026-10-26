// 솔라피(SOLAPI) 문자 발송 — kb-auth-notify 전용. Deno 전역에 의존하지 않아 Node(Web Crypto)로도 시험할 수 있다.
// 공식 문서: POST https://api.solapi.com/messages/v4/send-many/detail, 인증은 HMAC-SHA256(date + salt, apiSecret).
// 키·번호 같은 값은 코드에 두지 않는다(공개 저장소) — 전부 Supabase 시크릿에서 받는다.

export const SOLAPI_BASE = "https://api.solapi.com";

/** 문자를 보내는 알림 종류. 재개(resumed)·예고(pre_warning)는 텔레그램만 — 문자는 사람이 움직여야 할 때만. */
export const SMS_KINDS = new Set(["auth_required", "reminder", "timeout", "test"]);

type SolapiMsg = { messageId?: string; statusCode?: string; statusMessage?: string };
type SolapiSendResponse = {
  groupInfo?: { _id?: string };
  // 배열로 오기도, messageId 를 키로 한 객체로 오기도 한다(보픽 실측).
  messageList?: SolapiMsg[] | Record<string, SolapiMsg>;
  failedMessageList?: SolapiMsg[] | Record<string, SolapiMsg>;
};

export type SmsResult = { requested: number; failed: number; groupId: string | null; error: string | null };

function toHex(buf: ArrayBuffer): string {
  return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, "0")).join("");
}

function randomSalt(): string {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return toHex(bytes.buffer);
}

/** 솔라피 Authorization 헤더. date·salt 는 시험용으로만 주입한다. */
export async function solapiAuthHeader(
  apiKey: string,
  apiSecret: string,
  date: string = new Date().toISOString(),
  salt: string = randomSalt(),
): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(apiSecret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(date + salt));
  return `HMAC-SHA256 apiKey=${apiKey}, date=${date}, salt=${salt}, signature=${toHex(sig)}`;
}

/** 쉼표 구분 번호 목록 → 숫자만 남긴 휴대폰 번호(010…) 목록. 형식이 틀린 값은 버린다. */
export function parsePhones(raw: string | undefined | null): string[] {
  return String(raw ?? "")
    .split(",")
    .map((v) => v.replace(/\D/g, ""))
    .filter((v) => /^01[016789]\d{7,8}$/.test(v));
}

/** 한국 시간(KST) 기준 조용한 시간인가(기본 23시~07시). 밤에는 문자를 보내지 않고 07시 재알림에 맡긴다. */
export function isQuietKst(nowMs: number, startHour = 23, endHour = 7): boolean {
  const hour = new Date(nowMs + 9 * 60 * 60 * 1000).getUTCHours();
  return startHour > endHour ? hour >= startHour || hour < endHour : hour >= startHour && hour < endHour;
}

/** 문자 본문 — 짧게(가능하면 단문 90바이트 안쪽). 고정 템플릿만 쓴다. */
export function smsText(kind: string, row: number, waitedMin: number): string {
  const at = row > 0 ? `${row}행` : "현재 고객";
  switch (kind) {
    case "auth_required":
      return `[깐돌이] KB 재인증 필요. PC 인증창에서 본인인증 해주세요. 인증되면 ${at}부터 자동 재개`;
    case "reminder":
      return `[깐돌이] KB 재인증 대기 ${waitedMin}분째. PC 인증창에서 본인인증 해주세요(${at})`;
    case "timeout":
      return `[깐돌이] KB 재인증 대기 시간 초과로 작업이 멈췄습니다(${at}). 다시 시작해주세요`;
    case "test":
      return "[깐돌이] 알림 시험 문자입니다. KB 재인증 창이 뜨면 이 번호로 문자가 갑니다";
    default:
      throw new Error("unsupported sms kind");
  }
}

function listOf(v: SolapiMsg[] | Record<string, SolapiMsg> | undefined): SolapiMsg[] {
  if (!v) return [];
  return Array.isArray(v) ? v : Object.values(v);
}

/** 문자 발송. 실패해도 throw 하지 않고 결과로 돌려준다(알림 실패가 함수 전체를 죽이지 않게). */
export async function sendSolapiSms(opts: {
  apiKey: string;
  apiSecret: string;
  from: string;
  to: string[];
  text: string;
  fetchImpl?: typeof fetch;
}): Promise<SmsResult> {
  const doFetch = opts.fetchImpl ?? fetch;
  const from = opts.from.replace(/\D/g, "");
  if (!opts.to.length || !from) return { requested: 0, failed: 0, groupId: null, error: "no_recipient_or_sender" };
  try {
    const res = await doFetch(`${SOLAPI_BASE}/messages/v4/send-many/detail`, {
      method: "POST",
      headers: {
        Authorization: await solapiAuthHeader(opts.apiKey, opts.apiSecret),
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ messages: opts.to.map((to) => ({ to, from, text: opts.text })) }),
    });
    const bodyText = await res.text();
    let json: SolapiSendResponse & { errorCode?: string; errorMessage?: string } = {};
    try {
      json = bodyText ? JSON.parse(bodyText) : {};
    } catch { /* 본문이 JSON 이 아닐 수도 있다 */ }
    if (!res.ok) {
      return { requested: opts.to.length, failed: opts.to.length, groupId: null, error: json.errorCode || `solapi_${res.status}` };
    }
    const failed = listOf(json.failedMessageList);
    return {
      requested: opts.to.length,
      failed: failed.length,
      groupId: json.groupInfo?._id ?? null,
      error: failed.length ? (failed[0].statusCode || failed[0].statusMessage || "send_failed") : null,
    };
  } catch (e) {
    return { requested: opts.to.length, failed: opts.to.length, groupId: null, error: e instanceof Error ? e.name : "network_error" };
  }
}
