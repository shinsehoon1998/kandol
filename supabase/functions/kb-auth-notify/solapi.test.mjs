// kb-auth-notify/solapi.ts 시험 — 실행: node supabase/functions/kb-auth-notify/solapi.test.mjs (Node 22.6+ 타입 제거). 실제 발송 없음, fetch 는 가짜.
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import {
  isQuietKst, parsePhones, sendSolapiSms, smsText, solapiAuthHeader, SMS_KINDS,
} from "./solapi.ts";

// 1) 서명 = 보픽에서 실제 쓰는 Node createHmac 방식과 같아야 한다
const date = "2026-10-26T04:00:00.000Z", salt = "ab".repeat(32);
const h = await solapiAuthHeader("KEY", "SECRET", date, salt);
const expect = createHmac("sha256", "SECRET").update(date + salt).digest("hex");
assert.equal(h, `HMAC-SHA256 apiKey=KEY, date=${date}, salt=${salt}, signature=${expect}`);

// 2) 번호 파싱
assert.deepEqual(parsePhones("010-1234-5678, 01198765432, 1660-2426, abc,"), ["01012345678", "01198765432"]);
assert.deepEqual(parsePhones(undefined), []);

// 3) 조용한 시간(KST) — UTC 기준 ms 로 넣는다
const kst = (h, m = 0) => Date.UTC(2026, 9, 26, h - 9, m);
assert.equal(isQuietKst(kst(22, 59)), false);
assert.equal(isQuietKst(kst(23, 0)), true);
assert.equal(isQuietKst(kst(3, 0)), true);
assert.equal(isQuietKst(kst(6, 59)), true);
assert.equal(isQuietKst(kst(7, 0)), false);
assert.equal(isQuietKst(kst(14, 0)), false);

// 4) 문구 — EUC-KR 기준 바이트(한글 2, 그 밖 1). 단문 90바이트 넘으면 솔라피가 장문으로 보낸다(요금만 다름).
const bytes = (s) => [...s].reduce((n, c) => n + (c.charCodeAt(0) > 127 ? 2 : 1), 0);
for (const k of SMS_KINDS) {
  const t = smsText(k, 34, 12);
  console.log(k, bytes(t) + "바이트", bytes(t) <= 90 ? "단문" : "장문", "|", t);
}
assert.throws(() => smsText("resumed", 1, 1));

// 5) 발송 — 가짜 fetch
const calls = [];
const fake = (status, body) => async (url, init) => {
  calls.push({ url, init });
  return new Response(typeof body === "string" ? body : JSON.stringify(body), { status });
};
let r = await sendSolapiSms({ apiKey: "K", apiSecret: "S", from: "1660-2426", to: ["01012345678"], text: "t", fetchImpl: fake(200, { groupInfo: { _id: "G1" }, failedMessageList: [] }) });
assert.deepEqual(r, { requested: 1, failed: 0, groupId: "G1", error: null });
const sent = JSON.parse(calls[0].init.body);
assert.equal(calls[0].url, "https://api.solapi.com/messages/v4/send-many/detail");
assert.deepEqual(sent, { messages: [{ to: "01012345678", from: "16602426", text: "t" }] });
assert.match(calls[0].init.headers.Authorization, /^HMAC-SHA256 apiKey=K, date=.+, salt=[0-9a-f]{64}, signature=[0-9a-f]{64}$/);

r = await sendSolapiSms({ apiKey: "K", apiSecret: "S", from: "01000000000", to: ["01012345678", "01022223333"], text: "t", fetchImpl: fake(200, { groupInfo: { _id: "G2" }, failedMessageList: { m1: { statusCode: "1062", statusMessage: "발신번호 미등록" } } }) });
assert.deepEqual(r, { requested: 2, failed: 1, groupId: "G2", error: "1062" });

r = await sendSolapiSms({ apiKey: "K", apiSecret: "S", from: "01000000000", to: ["01012345678"], text: "t", fetchImpl: fake(403, { errorCode: "InvalidAPIKey" }) });
assert.deepEqual(r, { requested: 1, failed: 1, groupId: null, error: "InvalidAPIKey" });

r = await sendSolapiSms({ apiKey: "K", apiSecret: "S", from: "01000000000", to: ["01012345678"], text: "t", fetchImpl: async () => { throw new TypeError("net"); } });
assert.equal(r.failed, 1); assert.equal(r.error, "TypeError");

r = await sendSolapiSms({ apiKey: "K", apiSecret: "S", from: "", to: ["01012345678"], text: "t", fetchImpl: fake(200, {}) });
assert.equal(r.error, "no_recipient_or_sender");

console.log("ALL OK");
