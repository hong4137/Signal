/**
 * Signal — Threads 패널 수집 (Cloudflare Browser Rendering)
 *
 * 왜 브라우저가 필요한가
 *   threads.com 프로필은 본문을 JS 로 나중에 그린다. 정적 fetch 로 받으면
 *   278KB 짜리 빈 껍데기가 온다(실측). Apps Script 의 UrlFetchApp 도,
 *   Workers 의 기본 fetch 도 JS 를 실행하지 않으므로 같은 결과다.
 *
 * 이 시험이 확인하려는 것
 *   메타가 데이터센터 IP(=Cloudflare)를 막는지. 가정용 IP 에서는 열렸다.
 *   막히면 로그인 벽이 뜨고 본문이 안 나온다.
 *
 * 사용: GET /?h=jojoldu  또는  /?h=a,b,c  (쉼표로 여러 계정)
 *       Authorization: Bearer <ACCESS_KEY>   ← 필수
 *
 * 접근 보호
 *   workers.dev 주소는 공개다. 열어두면 남이 반복 호출해
 *   무료 브라우저 한도(하루 10분)를 태울 수 있다.
 *   ACCESS_KEY 시크릿이 없으면 **아무도** 못 쓴다(fail closed).
 *     npx wrangler secret put ACCESS_KEY
 *   로컬 `wrangler dev` 에서는 .dev.vars 에 ACCESS_KEY=... 로 둔다(커밋 금지).
 */
import puppeteer from "@cloudflare/puppeteer";

const UA =
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36";

const MAX_HANDLES = 6;

/** 길이 노출을 줄이려 전체를 훑는 비교 */
function safeEqual(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method !== "GET") {
      return json({ ok: false, error: "GET 만 허용" }, 405);
    }

    // ── 접근 검사 ── 시크릿이 없으면 잠긴 상태로 둔다
    if (!env.ACCESS_KEY) {
      return json({ ok: false, error: "ACCESS_KEY 미설정 — wrangler secret put ACCESS_KEY" }, 503);
    }
    const bearer = (request.headers.get("authorization") || "")
      .replace(/^Bearer\s+/i, "").trim();
    const given = bearer || url.searchParams.get("k") || "";
    if (!safeEqual(given, env.ACCESS_KEY)) {
      return json({ ok: false, error: "unauthorized" }, 401);
    }

    const handles = (url.searchParams.get("h") || "jojoldu")
      .split(",").map((s) => s.trim()).filter(Boolean).slice(0, MAX_HANDLES);
    const debug = url.searchParams.get("debug") === "1";

    let browser;
    try {
      browser = await puppeteer.launch(env.BROWSER);
    } catch (e) {
      return json({ ok: false, stage: "launch", error: String(e) }, 500);
    }

    const out = [];
    try {
      for (const h of handles) {
        const page = await browser.newPage();
        await page.setUserAgent(UA);
        await page.setViewport({ width: 1280, height: 1400 });
        const t0 = Date.now();
        let status = 0;
        try {
          const resp = await page.goto(`https://www.threads.com/@${h}`, {
            waitUntil: "networkidle0",
            timeout: 30000,
          });
          status = resp ? resp.status() : 0;
        } catch (e) {
          out.push({ h, error: "goto:" + String(e).slice(0, 120) });
          await page.close();
          continue;
        }

        const data = await page.evaluate(() => {
          const body = document.body ? document.body.innerText : "";
          // 로그인 벽 판별 — 본문이 안 나오면 이 문구들만 남는다
          const walled =
            /Instagram으로 계속하기|Continue with Instagram/.test(body) &&
            body.length < 900;
          return {
            title: document.title || "",
            len: body.length,
            walled,
            // 한글 문장이 있으면 본문이 그려진 것이다
            ko: (body.match(/[가-힣]{4,}/g) || []).length,
            text: body.slice(0, 9000),   // 글 여러 편이 들어가야 한다
          };
        });

        out.push({
          h,
          status,
          ms: Date.now() - t0,
          title: data.title,
          bodyLen: data.len,
          koChunks: data.ko,
          walled: data.walled,
          verdict: data.walled ? "BLOCKED(로그인 벽)"
                 : data.ko > 20 ? "OK(본문 렌더됨)"
                 : "UNCLEAR",
          text: debug ? data.text : data.text.slice(0, 900),
        });
        await page.close();
      }
    } finally {
      await browser.close();
    }
    return json({ ok: true, ua: "chrome", n: out.length, results: out });
  },
};

function json(o, s = 200) {
  return new Response(JSON.stringify(o, null, 1), {
    status: s,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}
