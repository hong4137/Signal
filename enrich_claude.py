#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Signal 보강기 — Claude 판 (2026-11-20, 사용자 결정 "균형안, Haiku")

enrich.py 가 SIGNAL_LLM 환경변수를 보고 이쪽으로 넘긴다.
  SIGNAL_LLM=claude (ANTHROPIC_API_KEY 가 있으면 기본) → 이 파일
  SIGNAL_LLM=gemini                                   → enrich.py 의 예전 Gemini 길(되돌리기용)

무료 Gemini 때 한도 탓에 줄였던 것을 푼다.
  · 댓글 12개(검색 순) → HN 이 매긴 순위대로 100개 안팎(최상위 댓글 + 답글 일부)
  · 원문 기사 본문도 읽는다(받을 수 있을 때만)
  · 쟁점 판정 — 무엇으로 갈렸나 · 양쪽 논리 · 대략 비율
  · 처음 본 순간 한 번 요약하고 끝 → 댓글이 2배 이상 늘면 다시 요약
  · 처리 상한 없음 — 논쟁·규모·조용한 중요 항목 전부
  · 국내 매칭 — 네이버 후보를 Claude 가 보고 '같은 사건'인 것만 남긴다
  · 신뢰층 RSS · HF 모델 · GitHub 신규에 한 줄 요약

바꾸지 않는 것: 규모(댓글 수)·논쟁도(r = 댓글÷업보트)는 collect.py 의 규칙 계산 그대로.
"한 점수로 뭉개지 않는다"는 설계 원칙이라 LLM 이 대신하지 않는다.
"""
import io
import json
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import anthropic

import enrich as E

MODEL = os.environ.get("SIGNAL_CLAUDE_MODEL", "claude-haiku-5-5")
# Claude Haiku 5.5 가격(1M 토큰당, 프롬프트 10만 토큰 이하). 사용량 로그로 월 비용을 다시 맞추는 데 쓴다.
PRICE_IN, PRICE_OUT = 0.10, 0.50
VERSION = 2                 # 캐시 판. 1 = Gemini 시절(댓글 12개) — 화면에 보이는 것은 한 번 다시 만든다
WANT_COMMENTS = 100
ARTICLE_CHARS = 12000
ONELINE_CACHE = os.path.join(E.DATA, "oneline_cache.json")

_usage = {"calls": 0, "in": 0, "out": 0, "refusal": 0, "fail": 0}


def _client():
    # 429·529(과부하)·5xx 는 SDK 가 간격을 두고 다시 부른다
    return anthropic.Anthropic(max_retries=3, timeout=120.0)


def _ask(client, *, system, content, schema, effort="low", max_tokens=6000):
    """구조화 출력 한 번. 성공하면 dict, 아니면 None(이유는 로그)."""
    try:
        r = client.messages.create(
            model=MODEL, max_tokens=max_tokens, system=system,
            output_config={"effort": effort,
                           "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIStatusError as e:
        _usage["fail"] += 1
        print("    [Claude] HTTP %s: %s" % (e.status_code, str(e.message)[:160]), file=sys.stderr)
        return None
    except anthropic.APIConnectionError as e:
        _usage["fail"] += 1
        print("    [Claude] 연결 실패: %s" % e, file=sys.stderr)
        return None

    _usage["calls"] += 1
    _usage["in"] += r.usage.input_tokens
    _usage["out"] += r.usage.output_tokens
    if r.stop_reason == "refusal":
        _usage["refusal"] += 1
        cat = getattr(r.stop_details, "category", None) if r.stop_details else None
        print("    [Claude] 거절(%s) — 이 항목은 건너뛴다" % cat, file=sys.stderr)
        return None
    if r.stop_reason == "max_tokens":
        _usage["fail"] += 1
        print("    [Claude] 길이 한도에서 잘림 — 건너뛴다", file=sys.stderr)
        return None
    text = "".join(b.text for b in r.content if b.type == "text")
    try:
        return json.loads(text)
    except Exception:
        _usage["fail"] += 1
        print("    [Claude] JSON 아님: %s" % text[:160], file=sys.stderr)
        return None


# ─────────────────────────────────────────────────────────────
# 재료 — 순위대로 댓글, 원문 본문
# ─────────────────────────────────────────────────────────────

def _clean(t):
    t = re.sub(r"<p>", "\n", t or "")
    t = re.sub(r"<[^>]+>", "", t)
    for a, b in (("&#x27;", "'"), ("&quot;", '"'), ("&amp;", "&"), ("&gt;", ">"),
                 ("&lt;", "<"), ("&#x2F;", "/")):
        t = t.replace(a, b)
    return re.sub(r"[ \t]+", " ", t).strip()


def hn_thread(sid, want=WANT_COMMENTS):
    """HN 이 화면에 보여주는 순서(=지지 순)대로 댓글을 모은다.

    Algolia items 는 본문 전체를 한 번에 주지만 순서가 작성순이다.
    순위는 Firebase 의 kids 순서가 정답이라, 둘을 짝지어 쓴다(요청 2회).
    최상위 댓글을 순위대로 넣고, 각자 밑에 답글 2개까지 붙인다 — 반박이 거기서 나온다.
    """
    try:
        tree = json.loads(E._get("https://hn.algolia.com/api/v1/items/%s" % sid, timeout=40))
        kids = json.loads(E._get("https://hacker-news.firebaseio.com/v0/item/%s.json" % sid,
                                 timeout=25)).get("kids") or []
    except Exception as e:
        print("    [댓글] %s 실패: %s" % (sid, e), file=sys.stderr)
        return [], ""
    by_id = {c["id"]: c for c in tree.get("children") or []}
    order = [by_id[k] for k in kids if k in by_id] or list(by_id.values())
    out = []
    for rank, c in enumerate(order, 1):
        t = _clean(c.get("text"))
        if len(t) < 40:
            continue
        out.append("#%d %s" % (rank, t[:600]))
        for rep in [x for x in (c.get("children") or []) if len(_clean(x.get("text"))) >= 40][:2]:
            out.append("   ↳ %s" % _clean(rep.get("text"))[:400])
        if len(out) >= want:
            break
    return out, _clean(tree.get("text") or "")      # Ask/Show HN 은 본문이 여기 있다


def article_text(url):
    """원문 본문 — 못 받으면 빈 문자열(유료벽·차단은 흔하다). HTML 만 다룬다."""
    if not url or "news.ycombinator.com" in url or re.search(r"\.(pdf|zip|mp4|png|jpg)$", url, re.I):
        return ""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; jfnb-signal/2.0)"})
        with urllib.request.urlopen(req, timeout=20) as r:
            if "html" not in (r.headers.get("content-type") or "html"):
                return ""
            raw = r.read(2_000_000).decode("utf-8", "replace")
    except Exception:
        return ""
    raw = re.sub(r"(?is)<(script|style|nav|header|footer|aside|noscript|svg)[^>]*>.*?</\1>", " ", raw)
    m = re.search(r"(?is)<(article|main)[^>]*>(.*?)</\1>", raw)
    body = m.group(2) if m and len(m.group(2)) > 2000 else raw
    text = re.sub(r"(?s)<[^>]+>", " ", body)
    for a, b in (("&nbsp;", " "), ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"),
                 ("&lt;", "<"), ("&gt;", ">")):
        text = text.replace(a, b)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:ARTICLE_CHARS] if len(text) > 400 else ""


# ─────────────────────────────────────────────────────────────
# 항목 하나 — 요약 + 사건 판정 + 쟁점
# ─────────────────────────────────────────────────────────────

ITEM_SYSTEM = E.SYSTEM + """

쟁점 판정 (debate)
- 댓글이 실제로 갈렸는지 먼저 본다(split). 대부분 같은 반응이면 split=false 로 두고
  consensus 에 대체로 어떤 반응인지 한 문장. 억지로 두 편을 만들지 않는다.
- 갈렸으면 axis 에 '무엇으로 갈렸나'를 한 구절로(예: "오픈소스 라이선스 변경이 배신인가").
- sides 는 2~3개. 각 편의 입장(stance)·근거 논리(logic, 한 문장)·대략 비율(share, 0~100 정수).
  비율은 댓글 수와 #순위(앞 순위일수록 많이 지지받은 댓글)로 어림한다. 합이 100 이 되게.
- 원문 기사가 주어지면 '기사가 말한 것'과 '댓글이 반응한 것'을 섞지 않는다.
  summary_ko 는 여전히 댓글 반응이 중심이다."""

ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "summary_ko": {"type": "string"},
        "is_event": {"type": "boolean"},
        "kr_query": {"type": "string"},
        "debate": {
            "type": "object",
            "properties": {
                "split": {"type": "boolean"},
                "axis": {"type": "string"},
                "consensus": {"type": "string"},
                "sides": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"stance": {"type": "string"}, "logic": {"type": "string"},
                                   "share": {"type": "integer"}},
                    "required": ["stance", "logic", "share"],
                    "additionalProperties": False}},
            },
            "required": ["split", "axis", "consensus", "sides"],
            "additionalProperties": False,
        },
    },
    "required": ["summary_ko", "is_event", "kr_query", "debate"],
    "additionalProperties": False,
}


def enrich_item(client, b):
    sid = E.hn_id(b)
    comments, selftext = hn_thread(sid) if sid else ([], "")
    art = article_text(b.get("url")) or selftext[:ARTICLE_CHARS]
    parts = ["제목: %s" % b["title"],
             "업보트 %d · 댓글 %d · r %.2f" % (b.get("points", 0), b.get("comments", 0), b.get("r", 0))]
    if art:
        parts.append("── 원문(앞부분) ──\n%s" % art)
    parts.append("── 댓글 %d개 (#숫자 = HN 순위, ↳ = 그 댓글의 답글) ──\n%s"
                 % (len(comments), "\n".join(comments) or "(댓글 없음)"))
    out = _ask(client, system=ITEM_SYSTEM, content="\n\n".join(parts),
               schema=ITEM_SCHEMA, effort="medium")
    if out is None:
        return None
    out["n_comments_read"] = len(comments)
    out["article_read"] = bool(art)
    return out


# ─────────────────────────────────────────────────────────────
# 국내 매칭 — 규칙 대신 판정
# ─────────────────────────────────────────────────────────────

MATCH_SCHEMA = {
    "type": "object",
    "properties": {"same_event": {"type": "array", "items": {"type": "integer"}}},
    "required": ["same_event"],
    "additionalProperties": False,
}


def naver_candidates(q, cid, sec, n=8):
    if not (cid and sec and q):
        return []
    import urllib.parse
    u = ("https://openapi.naver.com/v1/search/news.json?query=%s&display=%d&sort=sim"
         % (urllib.parse.quote(q), n))
    try:
        d = json.loads(E._get(u, timeout=20, headers={"X-Naver-Client-Id": cid,
                                                     "X-Naver-Client-Secret": sec}))
    except Exception as e:
        print("    [네이버] 실패: %s" % e, file=sys.stderr)
        return []
    out = []
    for x in d.get("items", []):
        t, desc = (_clean(x.get("title", "")), _clean(x.get("description", "")))
        out.append({"t": t, "desc": desc, "u": x.get("originallink") or x.get("link"),
                    "d": (x.get("pubDate") or "")[:16]})
    return out


def match_kr(client, b, rec, cid, sec):
    cands = naver_candidates(rec["kr_query"], cid, sec)
    if not cands:
        return []
    listing = "\n".join("[%d] %s — %s" % (i, c["t"], c["desc"][:140]) for i, c in enumerate(cands))
    out = _ask(client,
               system=("해외 개발자 커뮤니티 글과 한국 기사 후보를 비교한다. "
                       "같은 사건(같은 회사·제품·발표·사고)을 다룬 기사 번호만 고른다. "
                       "주제만 비슷한 기사(같은 분야의 다른 소식)는 고르지 않는다. 없으면 빈 배열."),
               content="해외 글: %s\n요약: %s\n\n한국 기사 후보:\n%s"
                       % (b["title"], rec["summary_ko"], listing),
               schema=MATCH_SCHEMA, effort="low", max_tokens=2000)
    if out is None:     # 판정이 실패하면 예전 규칙으로 거른다
        return E.naver_news(rec["kr_query"], cid, sec, n=3)[:2]
    keep = [cands[i] for i in out["same_event"] if 0 <= i < len(cands)][:2]
    return [{"t": c["t"], "u": c["u"], "d": c["d"], "why": ["Claude 판정"]} for c in keep]


# ─────────────────────────────────────────────────────────────
# 한 줄 요약 — 신뢰층 RSS · HF 모델 · GitHub 신규
# ─────────────────────────────────────────────────────────────

ONELINE_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {
        "type": "object",
        "properties": {"idx": {"type": "integer"}, "one_line": {"type": "string"}},
        "required": ["idx", "one_line"], "additionalProperties": False}}},
    "required": ["items"],
    "additionalProperties": False,
}

ONELINE_HINT = {
    "trust_recent": "신뢰할 만한 해설가의 글이다. 무엇에 대한 글이고 핵심 주장이 뭔지.",
    "models": "Hugging Face 인기 모델이다. 무슨 모델(크기·용도·누가 만들었나)인지.",
    "new_repos": "GitHub 신규 인기 저장소다. 무엇을 하는 도구인지.",
}


def oneline_layer(client, layer, items, cache):
    todo = [b for b in items if b.get("story_key") and b["story_key"] not in cache]
    for i in range(0, len(todo), 10):
        chunk = todo[i:i + 10]
        rows = []
        for j, b in enumerate(chunk):
            body = article_text(b.get("url")) if layer == "trust_recent" else ""
            rows.append("[%d] %s\n%s%s" % (j, b.get("title", ""), b.get("desc", "") or "",
                                           ("\n본문 앞부분: " + body[:2500]) if body else ""))
        out = _ask(client,
                   system=("각 항목을 한국어 한 문장(40자 안팎)으로 소개한다. 과장·홍보 문구 금지. "
                           "모르는 건 지어내지 않는다. " + ONELINE_HINT[layer]),
                   content="\n\n".join(rows), schema=ONELINE_SCHEMA, effort="low", max_tokens=3000)
        for r in (out or {}).get("items", []):
            if 0 <= r["idx"] < len(chunk):
                cache[chunk[r["idx"]]["story_key"]] = {"one_line": r["one_line"].strip()}
    for b in items:
        if b.get("story_key") in cache:
            b["one_line"] = cache[b["story_key"]]["one_line"]


# ─────────────────────────────────────────────────────────────
# 실행
# ─────────────────────────────────────────────────────────────

def _needs(k, b, cache):
    old = cache.get(k)
    if not old:
        return "새 글"
    if old.get("v", 1) < VERSION:
        return "Gemini 판 → 다시"
    before = old.get("comments_at") or 0
    if before and b.get("comments", 0) >= 2 * before:
        return "댓글 %d→%d" % (before, b["comments"])
    return None


def run(args):
    if not os.environ.get("ANTHROPIC_API_KEY") and not args.dry_run:
        print("ANTHROPIC_API_KEY 환경변수가 없다.", file=sys.stderr)
        sys.exit(1)
    latest = E.load_json(os.path.join(E.DATA, "latest.json"))
    if not latest:
        print("data/latest.json 이 없다. 먼저 collect.py 를 돌려라.", file=sys.stderr)
        sys.exit(1)
    cache = E.load_json(E.CACHE, {}) or {}
    ocache = E.load_json(ONELINE_CACHE, {}) or {}

    # 상한 없이 화면에 오르는 HN 항목 전부
    todo, seen = [], set()
    for arr in ("by_debate", "by_volume", "quiet_important"):
        for b in latest.get(arr, []):
            k = E.hn_id(b) or b.get("story_key")
            if not k or k in seen:
                continue
            seen.add(k)
            why = _needs(k, b, cache)
            if why:
                todo.append((k, b, why))
    lim = args.limit if args.limit and args.limit > 0 else len(todo)
    todo = todo[:lim]
    print("대상 %d건 (모델 %s)" % (len(todo), MODEL))
    for k, b, why in todo:
        print("  - [%s] %s  ← %s" % (k, b["title"][:56], why))
    if args.dry_run:
        return

    client = _client()
    cid, sec = E.naver_keys()
    t0 = time.time()

    def work(job):
        k, b, why = job
        rec = enrich_item(client, b)
        if rec is None:
            return k, None
        rec["kr"] = []
        if rec["is_event"] and rec["kr_query"].strip() and cid:
            rec["kr"] = match_kr(client, b, rec, cid, sec)
        rec.update({"v": VERSION, "comments_at": b.get("comments", 0),
                    "model": MODEL, "at": time.strftime("%Y-%m-%d %H:%M")})
        return k, rec

    got = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for k, rec in ex.map(work, todo):
            if rec is None:
                continue
            cache[k] = rec
            got += 1
            d = rec["debate"]
            print("  ✓ %s\n    %s\n    쟁점: %s" % (
                k, rec["summary_ko"][:110],
                (d["axis"] + " — " + " / ".join("%s %d%%" % (s["stance"][:24], s["share"])
                                                  for s in d["sides"]))
                if d["split"] else "(갈리지 않음) " + d["consensus"][:80]))

    for layer in ("trust_recent", "models", "new_repos"):
        oneline_layer(client, layer, latest.get(layer, []), ocache)

    io.open(E.CACHE, "w", encoding="utf-8").write(json.dumps(cache, ensure_ascii=False, indent=1))
    io.open(ONELINE_CACHE, "w", encoding="utf-8").write(json.dumps(ocache, ensure_ascii=False, indent=1))

    merged = 0
    for arr in ("by_debate", "by_volume", "quiet_important"):
        for b in latest.get(arr, []):
            b.pop("_comments", None)
            k = E.hn_id(b) or b.get("story_key")
            if k in cache:
                b.update({x: y for x, y in cache[k].items() if x not in ("at",)})
                merged += 1
    latest["enriched"] = True
    latest["enriched_at"] = time.strftime("%Y-%m-%d %H:%M")
    latest["enriched_by"] = MODEL
    io.open(os.path.join(E.DATA, "latest.json"), "w", encoding="utf-8").write(
        json.dumps(latest, ensure_ascii=False, indent=1))

    cost = (_usage["in"] * PRICE_IN + _usage["out"] * PRICE_OUT) / 1e6
    print("\n보강 완료 — 요약 %d건 · 병합 %d슬롯 · %.0f초" % (got, merged, time.time() - t0))
    # 첫 실행 사용량으로 월 비용을 다시 맞출 때 이 줄을 본다
    print("CLAUDE_USAGE calls=%d in=%d out=%d refusal=%d fail=%d cost=$%.4f model=%s"
          % (_usage["calls"], _usage["in"], _usage["out"], _usage["refusal"], _usage["fail"],
             cost, MODEL))
