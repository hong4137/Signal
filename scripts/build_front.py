#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
1면 조판 — 세 파이프라인을 한 면에 앉힌다.

  외신 브리핑  hong4137.github.io/briefing/briefings.json      사실
  Must News   hong4137.github.io/Must-News/data/ranking.json  국내
  Signal      data/latest.json · trend.json · panel_kr.json   반향

톱 선정 규칙
  **여러 소스에 동시에 걸린 것**이 톱이다. 교차보도 보너스를 파이프라인 단위로
  끌어올린 것. 외신 브리핑이 사실을 잡고 Signal 이 반향을 잡은 같은 사건이면
  한 칸에 둘을 같이 앉힌다.
  (2026-10-05 실증: '오픈AI 안전 책임자 사임' ↔ 'I quit OpenAI because its
   culture is broken' 댓글 772 · r 1.69)

Must News 는 섞지 않는다
  국내 매체 랭킹은 외신·Signal 과 거의 겹치지 않는다(10-05 상위 6건 교집합 0).
  하나의 순위로 섞지 말고 국내면을 따로 세운다.

사진
  외부 사진을 쓰지 않는다. 카드뉴스처럼 우리 데이터의 숫자를 그림으로 만든다.
  이 스크립트는 카드에 들어갈 숫자만 고르고, 그림은 front.html 이 그린다.

출력  data/front.json
"""
import io
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0 (compatible; SignalFront/1.0)"}

SRC_BRIEFING = "https://hong4137.github.io/briefing/briefings.json"
SRC_MUST = "https://hong4137.github.io/Must-News/data/ranking.json"
LINK_BRIEFING = "https://hong4137.github.io/briefing/archive/%s.html"
LINK_MUST = "https://hong4137.github.io/Must-News/"

# 패널 글은 사적인 일상도 섞인다(반응은 오히려 그쪽이 크다). 1면 해석 칸에는
# AI·개발 이야기만 올린다.
PANEL_TOPIC = re.compile(
    r"(AI|LLM|GPT|에이전트|agent|모델|클로드|Claude|제미나이|Gemini|오픈AI|OpenAI|"
    r"앤트로픽|Anthropic|코딩|개발|엔비디아|반도체|GPU|프롬프트|오픈소스|딥러닝|추론)", re.I)

# 한·영 개체 정규화 — 외신 브리핑(한글)과 HN 제목(영문)을 같은 이름으로 모은다
ENT = {
    "openai": ["openai", "오픈ai", "오픈에이아이", "chatgpt", "챗gpt", "altman", "알트먼", "알트만"],
    "anthropic": ["anthropic", "앤트로픽", "앤스로픽", "claude", "클로드", "dario", "다리오"],
    "google": ["google", "구글", "gemini", "제미나이", "deepmind", "딥마인드"],
    "meta": ["meta", "메타", "zuckerberg", "저커버그", "muse", "뮤즈"],
    "apple": ["apple", "애플", "iphone", "아이폰"],
    "nvidia": ["nvidia", "엔비디아", "jensen", "젠슨"],
    "microsoft": ["microsoft", "마이크로소프트", "copilot", "코파일럿"],
    "amazon": ["amazon", "아마존", "aws"],
    "deepseek": ["deepseek", "딥시크"],
    "xai": ["xai", "grok", "그록", "musk", "머스크"],
    "tesla": ["tesla", "테슬라"],
    "samsung": ["samsung", "삼성"],
    "sk": ["sk hynix", "sk하이닉스", "하이닉스"],
    "tsmc": ["tsmc"],
    "trump": ["trump", "트럼프", "white house", "백악관"],
    "qwen": ["qwen", "큐원", "alibaba", "알리바바"],
    "mistral": ["mistral", "미스트랄"],
    "lecun": ["lecun", "르쿤"],
}
# 영문 동작어 → 한글 대응 — 'quit' 과 '사임' 을 같은 것으로 본다
ACT = {
    "사임": ["quit", "resign", "resigned", "resigns", "leaving", "left", "사임", "퇴사", "떠나"],
    "인수": ["acquire", "acquisition", "buys", "인수"],
    "소송": ["lawsuit", "sue", "sued", "court", "소송", "법원", "판결"],
    "출시": ["launch", "launches", "release", "released", "unveil", "출시", "공개"],
    "해킹": ["hack", "hacked", "breach", "attack", "해킹", "유출", "공격"],
    "규제": ["regulat", "ban", "banned", "law", "규제", "금지", "법안"],
    "안전": ["safety", "risk", "extinction", "안전", "위험", "절멸"],
    "문화": ["culture", "toxic", "broken", "문화"],
}
STOP = {"있다", "했다", "한다", "이다", "대한", "위해", "통해", "관련", "최근", "이번", "지난",
        "가장", "일각", "다른", "편에서", "의견", "엇갈렸다", "있는", "하는", "되는", "에서",
        "으로", "에게", "부터", "까지", "그리고", "하지만", "또한", "모두", "것으로", "것이"}


def get(u):
    req = urllib.request.Request(u, headers=UA)
    return json.loads(urllib.request.urlopen(req, timeout=30).read().decode("utf-8"))


def load(path, default=None):
    try:
        return json.load(io.open(path, encoding="utf-8"))
    except Exception:
        return default


def tags(text):
    t = (text or "").lower()
    e = {k for k, vs in ENT.items() if any(v in t for v in vs)}
    a = {k for k, vs in ACT.items() if any(v in t for v in vs)}
    return e, a


def ko_nouns(text):
    return {w for w in re.findall(r"[가-힣]{2,}", text or "") if w not in STOP}


def sides(summary):
    """'일각에서는 A … 반면, 다른 편에서는 B' 를 두 진영으로 가른다.
    enrich.py 가 '양쪽 입장을 모두 적어라' 로 요약을 만들어서 가능한 일이다."""
    s = (summary or "").strip()
    for k in ("반면", "한편", "반대로"):
        if k in s:
            a, b = s.split(k, 1)
            a = re.sub(r"^.*?(일각에서는|일부는|한쪽에서는)\s*", "", a).strip(" ,.")
            b = re.sub(r"^[\s,]*(다른 (편|쪽)에서는|반대편에서는|다른 이들은)\s*", "", b).strip(" ,.")
            a = re.split(r"(?<=[다])\s", a)[-1] if len(a) > 90 else a
            if 8 <= len(a) and 8 <= len(b):
                return {"a": trim(a, 90), "b": trim(b, 90),
                        "la": stance(a, "한쪽"), "lb": stance(b, "다른 쪽")}
    return None


STANCE = ("옹호", "지지", "환영", "기대", "낙관", "비판", "반박", "우려", "회의", "경계",
          "지적", "반론", "의문", "냉소")


def stance(text, default):
    """진영 이름표 — 문장 끝에 가까운 입장 동사를 쓴다 ('…용기를 옹호한' → 옹호)."""
    best, pos = default, -1
    for w in STANCE:
        i = text.rfind(w)
        if i > pos:
            best, pos = w, i
    return best


def trim(s, n):
    s = re.sub(r"\.{2,}$", ".", (s or "").strip())
    if len(s) <= n:
        return s
    cut = s[: n - 1]
    sp = cut.rfind(" ")
    if sp > n * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,.") + "…"


def main():
    now = datetime.now(KST)
    latest = load(os.path.join(DATA, "latest.json"), {}) or {}
    trend = load(os.path.join(DATA, "trend.json"), {}) or {}
    panel = load(os.path.join(ROOT, "panel_kr.json"), {}) or {}

    try:
        B = get(SRC_BRIEFING)
        brief = (B.get("briefings") or [{}])[0]
    except Exception as e:
        print("  [외신 브리핑] 실패: %s" % e, file=sys.stderr)
        brief = {}
    try:
        M = get(SRC_MUST)
    except Exception as e:
        print("  [Must News] 실패: %s" % e, file=sys.stderr)
        M = {}

    segs = [s for s in (brief.get("segments") or []) if s.get("title")]
    sig, seen = [], set()
    for src in (latest.get("by_volume") or [], latest.get("by_debate") or []):
        for b in src:
            if b["title"] in seen:
                continue
            seen.add(b["title"])
            sig.append(b)

    def reaction(g):
        return {"title": g["title"], "url": g.get("discussion") or g.get("url"),
                "comments": g["comments"], "points": g["points"], "r": g["r"],
                "summary": g.get("summary_ko", ""), "sides": sides(g.get("summary_ko")),
                "trust": g.get("trust", []), "kr": (g.get("kr") or [])[:2]}

    def match(seg):
        """외신 한 꼭지 ↔ Signal 항목 중 같은 사건으로 볼 만한 최선."""
        st = seg.get("title", "") + " " + (seg.get("summary") or "")
        se, sa = tags(st)
        sk = ko_nouns(st)
        best = None
        for g in sig:
            ge, ga = tags(g["title"] + " " + (g.get("summary_ko") or ""))
            e, a = se & ge, sa & ga
            k = sk & ko_nouns(g.get("summary_ko") or "")
            # 회사 이름 하나만 겹치는 건 약하다(OpenAI 는 거의 매일 나온다).
            # 개체 + (동작 또는 한글 명사 2개) 가 함께 겹쳐야 같은 사건으로 본다.
            if not e or not (a or len(k) >= 2):
                continue
            score = g["comments"] * (1 + len(e) + 1.5 * len(a) + 0.4 * len(k))
            if not best or score > best[0]:
                best = (score, g, sorted(e), sorted(a | k)[:6])
        return best

    # ── 톱 선정: 외신 브리핑 × Signal 교차 ──
    matches = {i: match(s) for i, s in enumerate(segs[:8])}
    hit = [(m[0], i) for i, m in matches.items() if m]
    blink = LINK_BRIEFING % brief["date"] if brief.get("date") else None

    lead = None
    used_seg, used_sig = set(), set()
    if hit:
        _, si = max(hit)
        _, g, ents, shared = matches[si]
        s = segs[si]
        lead = {
            "kind": "cross",
            "headline": s["title"],
            "deck": trim(s.get("summary"), 220),
            "fact_src": "외신 브리핑 · %s" % brief.get("date", ""),
            "fact_url": blink,
            "reaction": reaction(g),
            "why": {"entities": ents, "shared": shared},
        }
        used_seg.add(si)
        used_sig.add(g["title"])
    else:
        cand = [g for g in sig if g.get("summary_ko")] or sig
        if cand:
            g = max(cand, key=lambda x: x["comments"])
            lead = {
                "kind": "signal",
                "headline": g["title"], "deck": trim(g.get("summary_ko"), 220),
                "fact_src": "Hacker News", "fact_url": g.get("discussion") or g.get("url"),
                "reaction": reaction(g),
            }
            used_sig.add(g["title"])

    # ── 좌측 레일: 외신 브리핑 나머지 ──
    left = []
    for i, sg in enumerate(segs):
        if i in used_seg:
            continue
        m = matches.get(i)
        echo = None
        if m and m[1]["title"] not in used_sig:
            echo = {"comments": m[1]["comments"], "r": m[1]["r"],
                    "url": m[1].get("discussion") or m[1].get("url")}
            used_sig.add(m[1]["title"])
        left.append({"title": sg["title"], "deck": trim(sg.get("summary"), 130),
                     "url": blink, "echo": echo})
    left = left[:4]

    # 외신이 다루지 않은 해외 반향 — 규모 상위 (요약 있는 것만)
    wire = [{"title": g["title"], "url": g.get("discussion") or g.get("url"),
             "deck": trim(g.get("summary_ko"), 90), "comments": g["comments"], "r": g["r"]}
            for g in sorted(sig, key=lambda x: -x["comments"])
            if g["title"] not in used_sig and g.get("summary_ko")][:3]
    for w in wire:
        used_sig.add(w["title"])

    # ── 하단 피처: Signal 논쟁 상위 (요약 있는 것 우선) ──
    feats = sorted([g for g in sig if g["title"] not in used_sig and g.get("summary_ko")],
                   key=lambda x: (-x["r"], -x["comments"]))[:2]
    features = [{
        "title": g["title"], "url": g.get("discussion") or g.get("url"),
        "deck": trim(g.get("summary_ko"), 140), "comments": g["comments"],
        "points": g["points"], "r": g["r"], "sides": sides(g.get("summary_ko")),
    } for g in feats]

    # ── 온도 카드: 가장 많이 오른 주제군 (완료된 날끼리) ──
    temp = None
    ser = [s for s in (trend.get("series") or []) if not s.get("partial")]
    if len(ser) >= 7:
        gs = [k for k in ser[0] if k not in ("date", "n", "ymd", "partial")]

        def ratio(g):
            a = sum(s[g] for s in ser[-3:]) / 3
            b = sum(s[g] for s in ser[-7:-3]) / 4
            return (a / b) if b > 0 else (9.9 if a > 0 else 0)
        top = max(gs, key=ratio)
        temp = {"group": top, "ratio": round(ratio(top), 1),
                "values": [s[top] for s in ser[-14:]],
                "dates": [s["date"] for s in ser[-14:]]}

    # ── 국내면: Must News ──
    arts = (M.get("articles") or [])[:6]
    dom_lead = max(arts, key=lambda a: a.get("outlets", 0)) if arts else None
    domestic = {
        "updated": M.get("updated", ""),
        "lead": {"title": dom_lead["title"], "url": dom_lead.get("url"),
                 "outlets": dom_lead.get("outlets", 0),
                 "sources": (dom_lead.get("sources") or [])[:12]} if dom_lead else None,
        "list": [{"rank": a.get("rank"), "title": a["title"], "url": a.get("url"),
                  "outlets": a.get("outlets", 0), "source": a.get("source", "")}
                 for a in arts if not dom_lead or a["title"] != dom_lead["title"]][:5],
    }

    # ── 해석 레일: 패널 + 신뢰층 ──
    voices = []
    for a in sorted(panel.get("accounts") or [], key=lambda x: x.get("tier", "C")):
        p = next((q for q in (a.get("posts") or []) if PANEL_TOPIC.search(q.get("t", ""))), {})
        if p.get("t"):
            voices.append({"kind": "panel", "who": a.get("n"), "handle": a.get("h"),
                           "tier": a.get("tier", "C"), "role": a.get("role", ""),
                           "text": trim(p["t"], 115), "ts": p.get("ts"),
                           "react": p.get("react", 0)})
        if len(voices) >= 3:
            break
    for q in (latest.get("quiet_important") or [])[:2]:
        voices.append({"kind": "trust", "who": " · ".join(q.get("trust", [])),
                       "text": q["title"], "url": q.get("discussion") or q.get("url"),
                       "comments": q["comments"], "r": q["r"]})

    # 호수 — 기록이 시작된 날부터 센다
    hist = load(os.path.join(DATA, "trend_history.json"), {}) or {}
    first = (hist.get("span") or [None])[0] or min((hist.get("days") or {}).keys() or [None])
    no = None
    if first:
        no = (now.date() - datetime.strptime(first, "%Y-%m-%d").date()).days + 1

    out = {
        "edition": now.strftime("%Y-%m-%d %H:%M"),
        "no": no,
        "sources": {"briefing": brief.get("date", ""), "must": M.get("updated", ""),
                    "signal": latest.get("updated", "")},
        "links": {"briefing": blink, "must": LINK_MUST},
        "lead": lead, "left": left, "wire": wire, "features": features, "temperature": temp,
        "domestic": domestic, "voices": voices,
    }
    io.open(os.path.join(DATA, "front.json"), "w", encoding="utf-8").write(
        json.dumps(out, ensure_ascii=False, indent=1))

    print("1면 조판 — %s" % out["edition"])
    if lead:
        print("  톱(%s): %s" % (lead["kind"], lead["headline"][:50]))
        if lead.get("why"):
            print("     교차 근거: 개체 %s · 공유 %s" % (lead["why"]["entities"], lead["why"]["shared"]))
    print("  좌측 %d(반향 %d) · 해외 %d · 피처 %d · 온도 %s · 국내 %d · 해석 %d" % (
        len(left), sum(1 for x in left if x["echo"]), len(wire), len(features), temp["group"] if temp else "-",
        len(domestic["list"]) + (1 if domestic["lead"] else 0), len(voices)))


if __name__ == "__main__":
    main()
