#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Signal Layer 보강기 — 한국어 요약 + 국내 보도 매칭

collect.py 가 만든 data/latest.json 을 읽어 각 항목에 붙인다.
  summary_ko  한국어 2~3문장. 기사 본문이 아니라 **HN 댓글**을 요약한다.
              본문은 영어로 읽을 수 있지만 "162개 댓글에서 뭐가 갈렸나"는
              다른 데서 얻을 수 없다. 그게 이 층의 값어치다.
  is_event    사건인가 주장인가. ★ 오탐 방지의 핵심 ★
  kr[]        사건일 때만 네이버 뉴스에서 찾은 국내 대응 보도

⚠️ is_event 판정이 왜 필요한가 (실측 근거)
   HN 논쟁 상위는 대부분 '주장·에세이'다 — "AI Has No Wisdom",
   "The LLMentalist Effect", "AI and the Destruction of the Creative Commons".
   국내 매체는 에세이를 번역해 싣지 않으므로 **대응 기사가 원래 없다.**
   여기에 키워드 매칭을 걸면 'military' 하나로
   "US Military had close call after using AI" ↔ "군 복무 대신 AI 연구…병역특례"
   같은 오탐이 나온다(실측). 주장으로 판정되면 검색을 아예 하지 않는다.

비용
   Gemini Flash 무료 등급 20 RPD. 기본 배치 3, 상한 12회.
   이미 보강한 항목은 data/enrich_cache.json 으로 건너뛴다.

사용
   setx GEMINI_API_KEY "..."        (네이버 키는 키 파일에서 자동으로 읽는다)
   python enrich.py                 기본
   python enrich.py --limit 10      상위 10개만
   python enrich.py --dry-run       Gemini 호출 없이 대상만 확인
"""
import argparse
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
CACHE = os.path.join(DATA, "enrich_cache.json")
NAVER_KEYFILE = r"d:/개발/네이버 API.txt"

# 무료 등급은 특정 모델이 통째로 과부하(503)에 걸리는 일이 잦다.
# 한 모델을 붙들고 재시도하지 말고 다음 모델로 넘어간다.
# 2026-09-23 실측: 플래시 계열 대부분이 503(광범위 과부하)인데 lite 는 열려 있었다.
# 최소 페이로드로도 503 이 났으므로 요청 문제가 아니다. 열린 것부터 시도한다.
# gemini-2.5-flash 는 404 — 신규 사용자에게 더 이상 제공되지 않는다. 넣지 말 것.
_pref = os.environ.get("SIGNAL_MODEL")
MODELS = [m for m in [_pref, "gemini-3.5-flash-lite", "gemini-3.5-flash",
                      "gemini-3.6-flash", "gemini-3.8-flash", "gemini-flash-latest"] if m]
MODELS = list(dict.fromkeys(MODELS))
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent"
RETRYABLE = {429, 500, 502, 503, 504}
UA = "windows:jfnb-signal:v1.0"

SYSTEM = """너는 한국 테크 기자의 리서치 보조다. 해외 개발자 커뮤니티(Hacker News)의
게시물과 그 댓글을 읽고, 한국어로 정리한다.

요약 원칙
- 기사 내용 자체보다 **댓글에서 무엇이 오갔는지**를 중심에 둔다.
  기자는 영어 원문을 읽을 수 있다. 그가 못 얻는 것은 '커뮤니티 반응'이다.
- 의견이 갈렸으면 양쪽 입장을 모두 적는다. 한쪽으로 정리하지 않는다.
- 2~3문장. 과장·홍보 문구 금지. 평서문.
- 확인되지 않은 내용을 지어내지 않는다. 댓글에 없으면 쓰지 않는다.

사건/주장 판정 (is_event)
- true  : 특정 시점에 실제로 일어난 일. 출시·발표·인수·소송·사고·인사·규제·실적.
- false : 주장·의견·에세이·튜토리얼·회고·벤치마크 해설·"내가 만들어봤다".
- 애매하면 false. 잘못 true 로 놓으면 엉뚱한 국내 기사가 붙는다.

한국어 검색어 (kr_query)
- is_event 가 true 일 때만 채운다. false면 빈 문자열.
- 한국 언론이 실제로 쓸 표기로. 영문 고유명사는 한글 표기를 앞세운다.
  Meta Muse → "메타 뮤즈", Qwen → "큐원", Anthropic → "앤트로픽".
- 3~6 단어. 너무 일반적이면(예: "AI 에이전트") 엉뚱한 결과가 나오니
  고유명사를 반드시 포함한다."""

SCHEMA = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {
            "idx": {"type": "INTEGER"},
            "title_echo": {"type": "STRING"},
            "summary_ko": {"type": "STRING"},
            "is_event": {"type": "BOOLEAN"},
            "kr_query": {"type": "STRING"},
        },
        "required": ["idx", "title_echo", "summary_ko", "is_event", "kr_query"],
    },
}


# ─────────────────────────────────────────────────────────────
# 공통
# ─────────────────────────────────────────────────────────────

def _get(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def load_json(p, default=None):
    try:
        return json.load(io.open(p, encoding="utf-8"))
    except Exception:
        return default


def hn_id(item):
    m = re.search(r"item\?id=(\d+)", item.get("discussion") or "")
    return m.group(1) if m else None


# ─────────────────────────────────────────────────────────────
# HN 댓글 — 논쟁의 실물
# ─────────────────────────────────────────────────────────────

def hn_comments(sid, want=12):
    url = ("https://hn.algolia.com/api/v1/search?tags=comment,story_%s"
           "&hitsPerPage=%d" % (sid, want * 3))
    try:
        d = json.loads(_get(url, timeout=25))
    except Exception as e:
        print("    [댓글] %s 실패: %s" % (sid, e), file=sys.stderr)
        return []
    out = []
    for h in d.get("hits", []):
        t = re.sub(r"<[^>]+>", "", h.get("comment_text") or "")
        t = (t.replace("&#x27;", "'").replace("&quot;", '"').replace("&amp;", "&")
              .replace("&gt;", ">").replace("&lt;", "<").replace("&#x2F;", "/"))
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) < 80:          # 한 줄 맞장구는 논쟁이 아니다
            continue
        out.append(t[:700])
        if len(out) >= want:
            break
    return out


# ─────────────────────────────────────────────────────────────
# Gemini
# ─────────────────────────────────────────────────────────────

_used = 0


def call_gemini(prompt, key, max_calls, tries=3):
    global _used
    if _used >= max_calls:
        return None, "budget-exhausted"
    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.25,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
            "responseSchema": SCHEMA,
        },
    }
    body = json.dumps(payload).encode()
    last = "?"
    for mi, model in enumerate(MODELS):          # 과부하면 다음 모델로
        for k in range(tries):
            if _used >= max_calls:
                return None, "budget-exhausted"
            _used += 1
            req = urllib.request.Request(
                ENDPOINT.format(m=model), data=body,
                headers={"x-goog-api-key": key, "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    j = json.loads(r.read().decode())
            except urllib.error.HTTPError as e:
                last = "http:%s" % e.code
                if e.code in RETRYABLE:
                    if k < tries - 1:
                        w = 8 * (k + 1) + random.uniform(0, 4)
                        print("    %s (%s) — %.0f초 후 재시도" % (last, model, w), file=sys.stderr)
                        time.sleep(w)
                        continue
                    break                         # 이 모델은 포기, 다음 모델
                return None, "%s %s" % (last, e.read()[:150].decode("utf-8", "replace"))
            except Exception as e:
                last = "net:%s" % type(e).__name__
                time.sleep(4 * (k + 1))
                continue

            cands = j.get("candidates") or []
            if not cands:
                return None, "no-candidate"
            fr = cands[0].get("finishReason", "")
            txt = "".join(p.get("text", "")
                          for p in cands[0].get("content", {}).get("parts", []))
            if fr and fr != "STOP":
                return None, "truncated:%s" % fr
            try:
                out = json.loads(txt)
                if mi:
                    print("    (모델 대체: %s)" % model, file=sys.stderr)
                return out, ""
            except Exception:
                return None, "parse"
    return None, "all-models-failed:%s" % last


def build_prompt(batch):
    """배치 안에서 항목이 섞이지 않도록 경계를 크게 두고 idx·제목을 되돌려받는다.
    (브리핑 deep_generate 가 배치 4에서 문단 오배분을 겪은 전례를 따른다)"""
    parts = []
    for i, it in enumerate(batch):
        cs = "\n".join("  - " + c for c in it["_comments"]) or "  (댓글 없음)"
        parts.append(
            "════════ 항목 %d ════════\n"
            "제목: %s\n업보트 %d · 댓글 %d · r %.2f\n"
            "── 댓글 ──\n%s" % (i, it["title"], it["points"], it["comments"], it["r"], cs))
    return ("아래 %d개 항목 각각에 대해 정리해라. idx 와 title_echo 를 반드시 그대로 되돌려라.\n\n"
            % len(batch)) + "\n\n".join(parts)


# ─────────────────────────────────────────────────────────────
# 네이버 뉴스 — 사건성 항목만
# ─────────────────────────────────────────────────────────────

def naver_keys():
    """환경변수 우선 — GitHub Actions 에는 로컬 키 파일이 없다.
    로컬에서는 기존 키 파일을 그대로 읽어 쓴다(설정 불필요)."""
    cid = os.environ.get("NAVER_CLIENT_ID")
    sec = os.environ.get("NAVER_CLIENT_SECRET")
    if cid and sec:
        return cid, sec
    try:
        raw = io.open(NAVER_KEYFILE, encoding="utf-8", errors="ignore").read()
    except Exception:
        return None, None
    toks = re.findall(r"[A-Za-z0-9_]{6,40}", raw)
    return (toks[0], toks[1]) if len(toks) >= 2 else (None, None)


_STOP_KO = {"인공지능", "에이아이", "모델", "기술", "서비스", "기업", "출시", "공개",
            "발표", "사용", "관련", "이슈", "논란", "문제", "시스템", "플랫폼"}


def _toks(s):
    """한글 2자 이상 + 영문 3자 이상. 흔한 일반어는 뺀다."""
    ko = [w for w in re.findall(r"[가-힣]{2,}", s or "") if w not in _STOP_KO]
    en = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", s or "")]
    return set(ko + en)


def _relevant(query, title, need=2):
    """질의와 결과 제목이 **2개 이상** 공유해야 통과.

    1개만 겹치면 버린다 — 실측 오탐:
      질의 '미국 국경 세관 휴대전화 수색'
      ↔ '호텔로 대마 보내고 관광객 위장 입국…세관 통제배달 수사에 덜미'
      공유어가 '세관' 하나뿐이었다. 없는 것보다 나쁜 매칭이므로 버린다.
    """
    qt, tt = _toks(query), _toks(title)
    shared = qt & tt
    if len(shared) >= need:
        return True, shared
    # 부분 포함도 인정 (뮤즈 ↔ 뮤즈가)
    hit = {q for q in qt if any(q in t or t in q for t in tt if len(t) >= 2)}
    return len(hit) >= need, hit


def naver_news(q, cid, sec, n=3):
    if not (cid and sec and q):
        return []
    u = ("https://openapi.naver.com/v1/search/news.json?query=%s&display=%d&sort=sim"
         % (urllib.parse.quote(q), n))
    try:
        raw = _get(u, timeout=20, headers={"X-Naver-Client-Id": cid,
                                           "X-Naver-Client-Secret": sec})
        d = json.loads(raw)
    except Exception as e:
        print("    [네이버] 실패: %s" % e, file=sys.stderr)
        return []
    out, dropped = [], []
    for x in d.get("items", []):
        t = re.sub(r"<[^>]+>", "", x.get("title", ""))
        for a, b in (("&quot;", '"'), ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                     ("&apos;", "'"), ("&#39;", "'")):
            t = t.replace(a, b)
        ok, shared = _relevant(q, t)
        if not ok:
            dropped.append(t[:40])
            continue
        out.append({"t": t, "u": x.get("originallink") or x.get("link"),
                    "d": (x.get("pubDate") or "")[:16],
                    "why": sorted(shared)[:4]})
    if dropped:
        print("    [네이버] 관련성 미달로 버림 %d건: %s"
              % (len(dropped), " / ".join(dropped[:2])), file=sys.stderr)
    return out


# ─────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────

def merge_into_latest(cache):
    latest = load_json(os.path.join(DATA, "latest.json")) or {}
    n = 0
    for arr in ("by_debate", "by_volume", "quiet_important"):
        for b in latest.get(arr, []):
            k = hn_id(b) or b.get("story_key")
            if k in cache:
                b.update(cache[k])
                n += 1
    latest["enriched"] = True
    latest["enriched_at"] = time.strftime("%Y-%m-%d %H:%M")
    io.open(os.path.join(DATA, "latest.json"), "w", encoding="utf-8").write(
        json.dumps(latest, ensure_ascii=False, indent=1))
    return n


def rematch():
    """요약은 그대로 두고 국내 검색만 다시. LLM 호출 0회."""
    cache = load_json(CACHE, {}) or {}
    cid, sec = naver_keys()
    if not cid:
        print("네이버 키를 못 읽었다.", file=sys.stderr)
        return
    hit = 0
    for k, v in cache.items():
        if not (v.get("is_event") and v.get("kr_query")):
            v["kr"] = []
            continue
        v["kr"] = naver_news(v["kr_query"], cid, sec, n=3)[:2]
        if v["kr"]:
            hit += 1
        time.sleep(0.3)
    io.open(CACHE, "w", encoding="utf-8").write(json.dumps(cache, ensure_ascii=False, indent=1))
    n = merge_into_latest(cache)
    ev = sum(1 for v in cache.values() if v.get("is_event"))
    print("재매칭 완료 — 사건 %d건 중 %d건에 국내 보도 연결 · %d슬롯 병합" % (ev, hit, n))
    for k, v in cache.items():
        if not v.get("is_event"):
            continue
        print("\n  질의: %s" % v["kr_query"])
        for a in v["kr"]:
            print("   🇰🇷 %s   ← 공유어 %s" % (a["t"][:58], ",".join(a.get("why", []))))
        if not v["kr"]:
            print("   🇰🇷 — 관련 보도 없음")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=14, help="보강할 항목 수")
    ap.add_argument("--batch", type=int, default=3, help="한 호출에 묶을 항목 수")
    ap.add_argument("--max-calls", type=int, default=12, help="Gemini 호출 상한 (무료 20 RPD)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rematch", action="store_true",
                    help="Gemini 호출 없이 캐시의 kr_query 로 국내 검색만 다시 돌린다")
    args = ap.parse_args()

    if args.rematch:
        return rematch()

    key = os.environ.get("GEMINI_API_KEY")
    if not key and not args.dry_run:
        print("GEMINI_API_KEY 환경변수가 없다.\n"
              '  setx GEMINI_API_KEY "..."  후 새 터미널에서 실행.', file=sys.stderr)
        sys.exit(1)

    latest = load_json(os.path.join(DATA, "latest.json"))
    if not latest:
        print("data/latest.json 이 없다. 먼저 collect.py 를 돌려라.", file=sys.stderr)
        sys.exit(1)
    cache = load_json(CACHE, {}) or {}

    # 논쟁 상위 + 규모 상위를 합집합으로 — 두 축 모두 대표되게
    pool, seen = [], set()
    for src in (latest.get("by_debate", []), latest.get("by_volume", [])):
        for b in src:
            if b["title"] in seen:
                continue
            seen.add(b["title"])
            pool.append(b)
    pool = pool[: args.limit * 2]

    todo, cached = [], 0
    for b in pool:
        sid = hn_id(b)
        k = sid or b.get("story_key")
        if not k:
            continue
        if k in cache:
            cached += 1
            continue
        todo.append((k, sid, b))
        if len(todo) >= args.limit:
            break

    print("대상 %d건 (캐시 적중 %d건, 호출 상한 %d회)" % (len(todo), cached, args.max_calls))
    if args.dry_run:
        for k, sid, b in todo:
            print("  - [%s] %s" % (sid or "?", b["title"][:62]))
        return

    # 댓글 수집
    for k, sid, b in todo:
        b["_comments"] = hn_comments(sid) if sid else []
        print("  댓글 %2d개 ← %s" % (len(b["_comments"]), b["title"][:52]))

    # Gemini
    got = {}
    for i in range(0, len(todo), args.batch):
        chunk = [b for _, _, b in todo[i:i + args.batch]]
        res, err = call_gemini(build_prompt(chunk), key, args.max_calls)
        if res is None:
            print("  [Gemini] 배치 %d 실패: %s" % (i // args.batch + 1, err), file=sys.stderr)
            if err == "budget-exhausted":
                break
            continue
        for r in res:
            j = r.get("idx", -1)
            if not (0 <= j < len(chunk)):
                continue
            # 오배분 가드 — 제목이 어긋나면 버린다
            if r.get("title_echo", "")[:24].lower() not in chunk[j]["title"].lower():
                print("  [경고] idx %d 제목 불일치 — 버림" % j, file=sys.stderr)
                continue
            got[chunk[j]["title"]] = r
        print("  [Gemini] 배치 %d — %d건 (누적 호출 %d)" % (i // args.batch + 1, len(res), _used))

    # 네이버 — 사건성만
    cid, sec = naver_keys()
    if not cid:
        print("  [네이버] 키 파일을 못 읽었다. 국내 매칭을 건너뛴다.", file=sys.stderr)
    ev = 0
    for k, sid, b in todo:
        r = got.get(b["title"])
        if not r:
            continue
        rec = {"summary_ko": r.get("summary_ko", "").strip(),
               "is_event": bool(r.get("is_event")),
               "kr_query": r.get("kr_query", "").strip(), "kr": []}
        if rec["is_event"] and rec["kr_query"]:
            rec["kr"] = naver_news(rec["kr_query"], cid, sec, n=2)
            ev += 1
            time.sleep(0.3)
        cache[k] = rec

    io.open(CACHE, "w", encoding="utf-8").write(
        json.dumps(cache, ensure_ascii=False, indent=1))

    # latest.json 에 병합
    merged = 0
    for arr in ("by_debate", "by_volume", "quiet_important"):
        for b in latest.get(arr, []):
            k = hn_id(b) or b.get("story_key")
            if k in cache:
                b.update(cache[k])
                merged += 1
    latest["enriched"] = True
    latest["enriched_at"] = time.strftime("%Y-%m-%d %H:%M")
    io.open(os.path.join(DATA, "latest.json"), "w", encoding="utf-8").write(
        json.dumps(latest, ensure_ascii=False, indent=1))

    print("\n보강 완료 — 요약 %d건 · 사건 판정 %d건 · Gemini 호출 %d회 · 병합 %d슬롯"
          % (len(got), ev, _used, merged))
    for k, sid, b in todo[:6]:
        r = cache.get(k)
        if not r:
            continue
        print("\n  ▸ %s" % b["title"][:64])
        print("    %s" % r["summary_ko"][:150])
        if r["kr"]:
            for a in r["kr"]:
                print("    🇰🇷 %s" % a["t"][:60])
        else:
            print("    🇰🇷 — (%s)" % ("검색 결과 없음" if r["is_event"] else "주장·에세이"))


if __name__ == "__main__":
    main()
