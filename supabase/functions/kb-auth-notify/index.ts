import { isQuietKst, parsePhones, sendSolapiSms, SMS_KINDS, smsText } from "./solapi.ts";

const ALLOWED_KINDS = new Set([
  "auth_required",
  "reminder",
  "resumed",
  "pre_warning",
  "timeout",
  "test", // 설치·설정 확인용 시험 알림(실행 중인 run_id 가 있어야 함)
]);
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const DEDUPE_WINDOW_MS = 2 * 60 * 1000;
const recentRequests = new Map<string, number>();

type AuthNotifyPayload = {
  kind: string;
  run_id: string;
  hwid: string;
  row: number;
  waited_min: number;
};

function json(body: Record<string, unknown>, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
      "Access-Control-Allow-Methods": "POST, OPTIONS",
    },
  });
}

function fixedMessage(payload: AuthNotifyPayload): string {
  const row = payload.row > 0 ? `${payload.row}행` : "현재 고객";
  const waited = `${payload.waited_min}분`;
  switch (payload.kind) {
    case "auth_required":
      return `[깐돌이] KB 재인증 필요 — PC의 인증방법 선택 창에서 본인인증을 해주세요. 인증되면 자동으로 이어서 진행합니다. (대기 ${waited}, ${row})`;
    case "reminder":
      return `[깐돌이] KB 재인증 대기 중 — PC의 인증방법 선택 창에서 본인인증을 완료해주세요. 인증되면 ${row}부터 자동 재개합니다. (대기 ${waited})`;
    case "resumed":
      return `[깐돌이] KB 재인증 완료 — ${row}부터 자동 재개했습니다. (대기 ${waited})`;
    case "pre_warning":
      return `[깐돌이] KB 세션이 곧 재인증될 수 있습니다. 현재 작업을 확인해주세요. (${row})`;
    case "timeout":
      return `[깐돌이] KB 재인증 대기 시간이 초과되어 작업이 멈췄습니다. (${row}, 대기 ${waited})`;
    case "test":
      return "[깐돌이] 알림 시험입니다. KB 재인증 창이 뜨면 이 채널로 알림이 갑니다.";
    default:
      throw new Error("unsupported kind");
  }
}

function validatePayload(value: unknown): AuthNotifyPayload {
  if (!value || typeof value !== "object") {
    throw new Error("JSON object required");
  }
  const input = value as Record<string, unknown>;
  const kind = typeof input.kind === "string" ? input.kind : "";
  const runId = typeof input.run_id === "string" ? input.run_id : "";
  const hwid = typeof input.hwid === "string" ? input.hwid.trim() : "";
  const row = input.row;
  const waited = input.waited_min;
  if (!ALLOWED_KINDS.has(kind)) throw new Error("invalid kind");
  if (!UUID_RE.test(runId)) throw new Error("invalid run_id");
  if (!hwid || hwid.length > 256) throw new Error("invalid hwid");
  if (!Number.isInteger(row) || Number(row) < 0 || Number(row) > 1_000_000) {
    throw new Error("invalid row");
  }
  if (!Number.isInteger(waited) || Number(waited) < 0 || Number(waited) > 24 * 60) {
    throw new Error("invalid waited_min");
  }
  return {
    kind,
    run_id: runId,
    hwid,
    row: Number(row),
    waited_min: Number(waited),
  };
}

function shouldSuppress(payload: AuthNotifyPayload): boolean {
  const now = Date.now();
  for (const [key, timestamp] of recentRequests) {
    if (now - timestamp >= DEDUPE_WINDOW_MS) recentRequests.delete(key);
  }
  const key = `${payload.run_id}:${payload.kind}`;
  const previous = recentRequests.get(key);
  if (previous !== undefined && now - previous < DEDUPE_WINDOW_MS) return true;
  recentRequests.set(key, now);
  return false;
}

// 공개 anon 키만 있으면 누구나 이 함수를 부를 수 있으므로, 실제로 돌고 있는 실행(run)에서 온 요청만 보낸다.
// Supabase Edge Function 기본 환경변수(SUPABASE_URL·SUPABASE_SERVICE_ROLE_KEY)로 execution_logs 를 확인한다.
async function isActiveRun(runId: string): Promise<boolean> {
  const url = Deno.env.get("SUPABASE_URL")?.trim();
  const key = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")?.trim();
  if (!url || !key) {
    console.error("Run check skipped: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing");
    return false;
  }
  const query = `${url.replace(/\/+$/, "")}/rest/v1/execution_logs?id=eq.${runId}&select=id,ended_at,started_at`;
  const response = await fetch(query, { headers: { apikey: key, Authorization: `Bearer ${key}` } });
  if (!response.ok) {
    console.error(`Run check failed: HTTP ${response.status}`);
    return false;
  }
  const rows = await response.json() as Array<{ ended_at: string | null; started_at: string | null }>;
  const run = rows[0];
  if (!run || run.ended_at) return false;
  const started = run.started_at ? Date.parse(run.started_at) : NaN;
  // 재인증 대기 최대 24시간 + 작업 시간 여유 → 72시간 넘은 실행은 거부
  return Number.isFinite(started) && Date.now() - started < 72 * 60 * 60 * 1000;
}

async function sendTelegram(text: string): Promise<number> {
  const token = Deno.env.get("TELEGRAM_BOT_TOKEN")?.trim();
  const chatIds = (Deno.env.get("KANDOL_ALERT_TELEGRAM_CHAT_IDS") ?? "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  if (!token || chatIds.length === 0) {
    console.info("Telegram notification skipped: required secrets are not configured");
    return 0;
  }

  let sent = 0;
  for (const chatId of chatIds) {
    const response = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ chat_id: chatId, text }),
    });
    if (!response.ok) {
      console.error(`Telegram notification failed: HTTP ${response.status}`);
      continue;
    }
    sent += 1;
  }
  return sent;
}

// 문자: 솔라피로 사람이 움직여야 할 알림(auth_required·reminder·timeout)만 보낸다. 밤(KST 23~07시)엔 보내지 않는다.
// 시크릿: SOLAPI_API_KEY, SOLAPI_API_SECRET, SOLAPI_SENDER(솔라피에 등록된 발신번호), KANDOL_ALERT_SMS_TO(쉼표 구분 수신번호).
async function sendSms(payload: AuthNotifyPayload): Promise<number> {
  if (!SMS_KINDS.has(payload.kind)) return 0;
  if (isQuietKst(Date.now())) {
    console.info("SMS skipped: quiet hours (KST 23-07)");
    return 0;
  }
  const apiKey = Deno.env.get("SOLAPI_API_KEY")?.trim();
  const apiSecret = Deno.env.get("SOLAPI_API_SECRET")?.trim();
  const from = Deno.env.get("SOLAPI_SENDER")?.trim();
  const to = parsePhones(Deno.env.get("KANDOL_ALERT_SMS_TO"));
  if (!apiKey || !apiSecret || !from || to.length === 0) {
    console.info("SMS skipped: SOLAPI secrets or recipients are not configured");
    return 0;
  }
  const result = await sendSolapiSms({
    apiKey,
    apiSecret,
    from,
    to,
    text: smsText(payload.kind, payload.row, payload.waited_min),
  });
  if (result.error) console.error(`SMS send failed: ${result.error} (${result.failed}/${result.requested})`);
  return result.requested - result.failed;
}

Deno.serve(async (request) => {
  if (request.method === "OPTIONS") return json({ ok: true });
  if (request.method !== "POST") return json({ error: "method_not_allowed" }, 405);

  try {
    const payload = validatePayload(await request.json());
    if (!(await isActiveRun(payload.run_id))) return json({ error: "unknown_run" }, 403);
    if (shouldSuppress(payload)) return json({ ok: true, deduped: true });
    const text = fixedMessage(payload);
    const [telegramSent, smsSent] = await Promise.all([
      sendTelegram(text),
      sendSms(payload),
    ]);
    return json({ ok: true, telegram_sent: telegramSent, sms_sent: smsSent });
  } catch (error) {
    console.error(`KB auth notification rejected: ${error instanceof Error ? error.message : "unknown error"}`);
    return json({ error: "invalid_request" }, 400);
  }
});
