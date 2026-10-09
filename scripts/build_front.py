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
import html
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
WEAK_ACT = {"안전", "규제", "출시", "문화"}     # 거의 모든 AI 기사에 나오는 동작어
STOP = {"있다", "했다", "한다", "이다", "대한", "위해", "통해", "관련", "최근", "이번", "지난",
        "가장", "일각", "다른", "편에서", "의견", "엇갈렸다", "있는", "하는", "되는", "에서",
        "으로", "에게", "부터", "까지", "그리고", "하지만", "또한", "모두", "것으로", "것이"}


# 클라우드 루틴(편집국)은 hong4137.github.io 에 접속할 수 없다(샌드박스 403).
# 대신 외신 브리핑·Must News 리포를 받아 두고 경로를 넘긴다.
#   BRIEFING_DIR = hong4137/briefing 클론 경로
#   MUST_DIR     = hong4137/Must-News 클론 경로
# 둘 다 없으면 지금처럼 웹에서 받는다 (GitHub Actions).
BRIEFING_DIR = os.environ.get("BRIEFING_DIR")
MUST_DIR = os.environ.get("MUST_DIR")


def get_local_or_web(local, url):
    if local and os.path.exists(local):
        return json.load(io.open(local, encoding="utf-8"))
    return get(url)


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


EN_STOP = {"with", "from", "that", "this", "have", "after", "about", "their", "your", "into",
           "over", "what", "when", "will", "says", "said", "they", "were", "been", "more",
           "than", "just", "only", "also", "being", "could", "would", "should", "because"}


def en_words(text):
    return {w for w in re.findall(r"[a-z][a-z0-9]{3,}", (text or "").lower()) if w not in EN_STOP}


def _txt(h):
    return html.unescape(re.sub(r"<[^>]+>", "", h or "")).strip()


def photo(url, w, h):
    """Unsplash 주소의 크기만 바꾼다 — 같은 사진을 자리에 맞게 다시 받는다."""
    if not url:
        return None
    url = re.sub(r"([?&])w=\d+", r"\g<1>w=%d" % w, url)
    return re.sub(r"([?&])h=\d+", r"\g<1>h=%d" % h, url)


# ── 자료사진 보관함 ──
# 그날 사진이 없는 기사에는 data/photo_catalog.json 에서 '자료사진'을 고른다.
# 카탈로그 = 외신 브리핑이 쓴 Unsplash 사진 97장을 **눈으로 검수**해 다시 태그한 81장.
# 브리핑이 붙인 분류·설명은 믿지 않는다 — 실측하니 '회로 기판' 설명에 애니 그림,
# '세계 지도' 설명에 물류창고가 걸려 있었다. 카탈로그에 없는 사진은 쓰지 않는다.
#
# (기사에 이런 말이 있으면 → 이 태그의 사진)  위에서부터 우선
PHOTO_TOPIC = [
    (r"트럼프|Trump", "trump"),
    (r"구글|Google", "google"),
    (r"테슬라|Tesla", "tesla"),
    (r"스페이스X|SpaceX", "spacex"),
    (r"로봇|휴머노이드", "robot"),
    (r"전기차|\bEV\b|사이버캡|로보택시|자율주행|자동차", "car"),
    (r"우주|위성|로켓", "space"),
    (r"드론|국방|방산|군사|미사일", "drone"),
    (r"헬스|의료|신약|바이오|제약|병원", "health"),
    (r"데이터센터|서버", "datacenter"),
    (r"반도체|노광|ASML|RISC|파운드리|웨이퍼|GPU|칩", "chip"),
    (r"감시|번호판|해킹|보안|취약점|바운티|유출|사이버", "security"),
    (r"법원|판사|소송|위헌|판결|재판", "court"),
    (r"주가|IPO|상장|증시|투자|펀드|버블|조정", "finance"),
    (r"쇼핑|이커머스|테무|소매|배송|무역|관세|수출|물류", "trade"),
    (r"플랫폼|앱스토어|메신저|SNS|소셜|로블록스|스팀", "social"),
    (r"규제|법안|당국|정부|의회|위원회|백악관", "government"),
    (r"데이터|프라이버시|개인정보", "data"),
    (r"오픈소스|코드|개발자|깃허브", "code"),
    (r"인수|합병|CEO|경영|사임|퇴사", "business"),
    (r"신학|종교|학자|연구진|대학", "research"),
]
PHOTO_SEC = {"AI/기술": "ai", "경제/금융": "finance", "반도체/인프라": "chip",
             "정책/플랫폼": "government", "하드웨어/기타": "hardware", "Claude's Pick": "finance"}
PHOTO_ANY = ["ai", "code", "data"]


def photo_off():
    """편집국이 발행 때 사진을 직접 열어 보고 끈 주소 — data/photo_off.json {"off": {url: {reason, date}}}.
    죽은 주소(404·이미지 아님)나 설명과 다른 사진으로 바뀐 주소가 여기 쌓인다. 여기 있는 사진은 어디에도 쓰지 않는다."""
    return set(((load(os.path.join(DATA, "photo_off.json"), {}) or {}).get("off") or {}).keys())


def photo_pool(_B=None):
    cat = load(os.path.join(DATA, "photo_catalog.json"), {}) or {}
    off = photo_off()
    return [x for x in cat.get("photos") or [] if x["url"] not in off]


def pick_photo(pool, it, sec, used):
    """기사 → 주제 태그가 맞는 자료사진 한 장. 같은 면에서 겹치지 않게 고른다."""
    text = " ".join((it.get("head", ""), it.get("sub", ""), it.get("en", ""), it.get("deck", "")))
    wants = [t for pat, t in PHOTO_TOPIC if re.search(pat, text, re.I)]
    wants += [PHOTO_SEC.get(sec, "ai")] + PHOTO_ANY
    seed = sum(ord(ch) for ch in (it.get("en") or it.get("head") or ""))
    for want in wants:
        cand = [x for x in pool if x["url"] not in used and want in x["tags"]]
        if cand:
            ph = cand[seed % len(cand)]
            used.add(ph["url"])
            return {"url": ph["url"], "alt": ph["desc"], "credit": ph.get("credit", ""),
                    "credit_url": ph.get("credit_url", ""), "tag": want}
    return None


def newsroom_pool():
    nr = load(os.path.join(DATA, "newsroom_photos.json"), {}) or {}
    comps = {c["id"]: c for c in nr.get("companies") or []}
    off = photo_off()
    return comps, [x for x in nr.get("photos") or [] if x["url"] not in off]


def pick_newsroom(nr, text, used):
    """기사가 뉴스룸 사진이 있는 회사를 다루면 그 회사 공식 사진 — 주제가 맞는 것 먼저, 없으면 대표 사진.
    회사 이름이 제목·부제에 있어야 한다(요약에만 스치는 회사는 주인공이 아니다).
    제품 사진("match" 가 있는 사진)이 먼저다 — 제목에 그 제품 이름이 나오면 회사 이름이 없어도 1대1로 붙인다."""
    comps, photos = nr
    def out(ph, c):
        used.add(ph["url"])
        cr = ph.get("credit") or c["credit"]
        return {"src": ph["url"], "sm": ph["url"], "alt": ph["desc"], "nr": cr,
                "credit": cr, "credit_url": ph.get("page", "")}
    title = text.get("title", "")
    best = None                                 # 이름이 가장 길게 맞는 제품 — '맥미니 M6' 가 '맥미니' 보다 먼저
    for ph in photos:
        c = comps.get(ph["company"]) or {}
        if not ph.get("match") or c.get("permitted") is False or ph["url"] in used:
            continue
        m = re.search(ph["match"], title, re.I)
        if m and (best is None or len(m.group(0)) > best[0]):
            best = (len(m.group(0)), ph, c)
    if best:
        return out(best[1], best[2])
    for cid, c in comps.items():
        if c.get("permitted") is False:            # 이용 조건 불명확 — 사람이 확인하고 켠다
            continue
        if not re.search(c["match"], text.get("title", "")):
            continue
        cand = [x for x in photos if x["company"] == cid and x["url"] not in used]
        body = " ".join(text.values())
        cand = [x for x in cand if not x.get("match")]   # 제품 사진은 그 제품 기사에만
        # 주제가 맞는 사진 중 가장 길게 맞는 것 ('HBM4' 가 '양산' 보다 구체적이다), 제목에서 맞으면 더 우선
        score = lambda x: max([(2, len(m.group(0))) for m in [re.search(x["topic"], text.get("title", ""), re.I)] if m]
                              + [(1, len(m.group(0))) for m in [re.search(x["topic"], body, re.I)] if m] + [(0, 0)])
        hit = sorted([x for x in cand if x.get("topic") and score(x) > (0, 0)], key=score, reverse=True)
        ph = (hit or [x for x in cand if x.get("default")] or cand or [None])[0]
        if ph:
            return out(ph, c)
    return None


def _grab(pat, body):
    m = re.search(pat, body, re.S)
    return _txt(m.group(1)) if m else ""


def briefing_articles(date):
    """외신 브리핑 상세 페이지의 기사 카드 전부.

    briefings.json 에는 요약 꼭지 3개뿐이고, 실제 기사(하루 20여 건)는
    archive/<날짜>.html 에만 있다. 카드마다 id="art-N" 이 붙어 있고
    그 사이트의 app.js 가 #art-N 으로 들어오면 해당 카드로 스크롤·강조한다.
    """
    url = LINK_BRIEFING % date
    try:
        local = BRIEFING_DIR and os.path.join(BRIEFING_DIR, "archive", "%s.html" % date)
        if local and os.path.exists(local):
            s = io.open(local, encoding="utf-8").read()
        else:
            req = urllib.request.Request(url, headers=UA)
            s = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    except Exception as e:
        print("  [외신 상세] 실패: %s" % e, file=sys.stderr)
        return []
    out, sec = [], ""
    for m in re.finditer(r'<h2 class="section-title">(.*?)</h2>'
                         r'|<article class="article-card[^"]*"([^>]*)>(.*?)</article>', s, re.S):
        if m.group(1) is not None:
            sec = re.sub(r"^[^\w가-힣']+", "", _txt(m.group(1))).strip()
            sec = "TOP" if "TOP" in sec.upper() else sec
            continue
        attrs, body = m.group(2), m.group(3)
        aid = re.search(r'id="(art-\d+)"', attrs)
        cu = re.search(r'data-card-url="([^"]*)"', attrs)
        en = _grab(r'class="article-title">(.*?)</h3>', body)
        ko = _grab(r'class="article-title-kr">(.*?)</p>', body)
        head, _, sub = ko.partition(" — ")
        out.append({
            "id": aid.group(1) if aid else None, "sec": sec,
            "badge": _grab(r'class="badge[^"]*">(.*?)</span>', body),
            "en": en, "head": head.strip() or en, "sub": sub.strip(),
            "summary": _grab(r'class="article-summary">(.*?)</p>', body),
            "source": _grab(r'class="source">(.*?)</span>', body),
            "url": html.unescape(cu.group(1)) if cu else None,
            "link": url + ("#" + aid.group(1) if aid else ""),
        })
    return out


def angles(story, n=5):
    """같은 사안을 매체마다 어떻게 제목 달았나 — 매체당 하나, 대표 제목과 겹치지 않는 것.
    Must News 가 묶은 기사(members)에서 고른다. '[속보]' 같은 말머리는 뗀다."""
    seen, out = {story.get("source")}, []
    base = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", story.get("title", "")))
    for m in story.get("members") or []:
        src, t = m.get("source", ""), re.sub(r"^\s*\[[^\]]{1,8}\]\s*", "", m.get("title", "")).strip()
        if not t or src in seen:
            continue
        words = set(re.findall(r"[가-힣A-Za-z0-9]{2,}", t))
        if words and len(words & base) / len(words) > 0.7:
            continue                      # 대표 제목을 거의 그대로 받아쓴 것
        shared = words & base
        if len(shared) < 2 and not any(len(w) >= 3 for w in shared):
            continue                      # 같은 묶음이어도 다른 사건이다 (10-06 엔비디아 OLED 묶음에 한컴·장관 기사)
        seen.add(src)
        out.append({"source": src, "title": t, "url": m.get("url")})
        if len(out) >= n:
            break
    return out


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
        B = get_local_or_web(BRIEFING_DIR and os.path.join(BRIEFING_DIR, "briefings.json"), SRC_BRIEFING)
        brief = (B.get("briefings") or [{}])[0]
    except Exception as e:
        print("  [외신 브리핑] 실패: %s" % e, file=sys.stderr)
        B, brief = {}, {}
    try:
        M = get_local_or_web(MUST_DIR and os.path.join(MUST_DIR, "data", "ranking.json"), SRC_MUST)
    except Exception as e:
        print("  [Must News] 실패: %s" % e, file=sys.stderr)
        M = {}

    blink = LINK_BRIEFING % brief["date"] if brief.get("date") else None

    # ── 편집 데스크 (data/desk.json) ──
    # 편집국 루틴(클로드)이 매일 아침 적어두는 손질. 없으면 원본 그대로 낸다.
    # 키: 외신 기사 = "<브리핑 날짜>#art-N", 커뮤니티 글 = 토론 URL.
    #   headline  제목 다시 쓰기 ("\n" = 줄바꿈 위치)
    #   sub       부제
    #   drop_photo  사진이 기사와 안 맞음 → 빼거나 자료사진으로 교체
    #   drop_echo   붙은 커뮤니티 반향이 다른 사건임 → 뗀다
    # "lead": 키 → 톱으로 올릴 외신 기사
    desk = load(os.path.join(DATA, "desk.json"), {}) or {}
    dx = desk.get("items") or {}
    arts = briefing_articles(brief.get("date")) if brief.get("date") else []
    for x in arts:
        x["key"] = "%s#%s" % (brief.get("date"), x["id"]) if x.get("id") else None
    if not arts:
        # 상세 페이지를 못 읽으면 요약 꼭지 3개로라도 짠다
        arts = [{"id": None, "sec": "TOP", "badge": "", "en": "", "head": sg["title"], "sub": "",
                 "summary": sg.get("summary") or "", "source": "", "url": None, "link": blink}
                for sg in (brief.get("segments") or []) if sg.get("title")]

    # ── 사진: 외신 브리핑이 이미 골라둔 것만 쓴다 (Unsplash) ──
    # 요약 꼭지(segments) 순서 = TOP 기사 순서(art-0..2). 그날의 히어로 사진은
    # 주제(hero_source)가 같은 꼭지에 준다 — 저작자 표기가 붙은 건 히어로뿐이다.
    tops = [i for i, x in enumerate(arts) if x["sec"] == "TOP"]
    # 금지 사진(photo_catalog.json "banned" — 예: 랜선 꽂힌 스위치)은 브리핑이 골라도 쓰지 않는다
    banned = {re.sub(r"^.*photo-|\?.*$", "", b["url"]) for b in
              ((load(os.path.join(DATA, "photo_catalog.json"), {}) or {}).get("banned") or [])}
    banned |= {re.sub(r"^.*photo-|\?.*$", "", u) for u in photo_off()}
    is_banned = lambda u: any(b and b in (u or "") for b in banned)
    for i, sg in zip(tops, brief.get("segments") or []):
        if sg.get("thumb_url") and not is_banned(brief.get("hero_url") if (brief.get("hero_url") and sg.get("thumb_category") == brief.get("hero_source")) else sg["thumb_url"]):
            hero = brief.get("hero_url") and sg.get("thumb_category") == brief.get("hero_source")
            arts[i]["img"] = {
                "src": photo(brief["hero_url"] if hero else sg["thumb_url"], 1200, 675),
                "sm": photo(brief["hero_url"] if hero else sg["thumb_url"], 640, 360),
                "alt": (brief.get("hero_alt") if hero else sg.get("thumb_alt")) or "",
                "credit": brief.get("hero_credit_name", "") if hero else "",
                "credit_url": brief.get("hero_credit_url", "") if hero else "",
            }
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

    def pair(art, g):
        """외신 기사 한 건 ↔ Signal 항목 한 건이 같은 사건인가. 아니면 None."""
        st = " ".join((art["head"], art["sub"], art["en"], art["summary"]))
        se, sa = tags(st)
        ge, ga = tags(g["title"] + " " + (g.get("summary_ko") or ""))
        e, a = se & ge, sa & ga
        k = (ko_nouns(st) & ko_nouns(g.get("summary_ko") or "")) | \
            (en_words(art["en"]) & en_words(g["title"]))
        # 회사 이름 하나만 겹치는 건 약하다(OpenAI 는 거의 매일 나온다).
        # 개체 + (강한 동작 / 약한 동작+공유 단어 / 공유 단어 2개) 여야 같은 사건으로 본다.
        # '안전'·'규제' 같은 일반 동작어만으로는 안 된다 — 10-05 실측:
        # 중국 'AI 연인' 규제 ↔ Qwen 로컬 구동 글이 'qwen'+'위험' 으로 붙었다.
        strong = a - WEAK_ACT
        if not e or not (strong or (a and k) or len(k) >= 2):
            return None
        score = g["comments"] * (1 + len(e) + 1.5 * len(a) + 0.4 * len(k))
        return (score, sorted(e), sorted(a | k)[:6])

    # 모든 (기사, 반향) 쌍을 점수순으로 세우고 한 번씩만 짝짓는다
    pairs = []
    for ai, art in enumerate(arts):
        for gi, g in enumerate(sig):
            m = pair(art, g)
            if m:
                pairs.append((m[0], ai, gi, m[1], m[2]))
    pairs.sort(key=lambda x: -x[0])
    echo_of, taken = {}, set()
    for score, ai, gi, ents, shared in pairs:
        if ai in echo_of or gi in taken:
            continue
        echo_of[ai] = (score, sig[gi], ents, shared)
        taken.add(gi)

    def echo(ai):
        m = echo_of.get(ai)
        if not m:
            return None
        g = m[1]
        return {"title": g["title"], "comments": g["comments"], "r": g["r"],
                "url": g.get("discussion") or g.get("url")}

    # ── 톱 선정: 외신 × 반향 교차. 편집자가 TOP 으로 고른 기사에 가점 ──
    lead = None
    used_art, used_sig = set(), set()
    cand = [(m[0] * (1.5 if arts[ai]["sec"] == "TOP" else 1), ai) for ai, m in echo_of.items()]
    pick = next((i for i, x in enumerate(arts) if desk.get("lead") and x.get("key") == desk["lead"]), None)
    if pick is None and cand:
        pick = max(cand)[1]
    if pick is not None:
        ai = pick
        m = echo_of.get(ai)
        art = arts[ai]
        lead = {
            "kind": "cross" if m else "desk",
            "key": art.get("key"),
            "headline": art["head"], "sub": art["sub"],
            "deck": trim(art["summary"], 260),
            "fact_src": "외신 브리핑" + (" · " + art["source"] if art["source"] else ""),
            "fact_url": art["link"], "orig_url": art["url"], "img": art.get("img"),
            "reaction": reaction(m[1]) if m else None,
            "why": {"entities": m[2], "shared": m[3]} if m else None,
        }
        used_art.add(ai)
        if m:
            used_sig.add(m[1]["title"])
    else:
        cg = [g for g in sig if g.get("summary_ko")] or sig
        if cg:
            g = max(cg, key=lambda x: x["comments"])
            lead = {
                "kind": "signal",
                "headline": g["title"], "deck": trim(g.get("summary_ko"), 220),
                "fact_src": "Hacker News", "fact_url": g.get("discussion") or g.get("url"),
                "reaction": reaction(g),
            }
            used_sig.add(g["title"])
    for ai in echo_of:
        used_sig.add(echo_of[ai][1]["title"])

    # ── 톱의 관련 기사 — 신문이 톱 아래 다는 '관련' 목록 ──
    # 같은 회사·기관(개체)을 다루고 한글 명사도 겹치는 외신 기사 최대 3건. 편집국이 desk.json
    # "related": [키…] 로 지정하면 그걸 쓴다.
    if lead and lead.get("key"):
        lt = " ".join((lead["headline"], lead.get("sub", ""), lead.get("deck", "")))
        le, _ = tags(lt)
        lk = ko_nouns(lt)
        scored = []
        for x in arts:
            if x.get("key") == lead["key"]:
                continue
            xt = " ".join((x["head"], x["sub"], x["en"], x["summary"]))
            xe, _ = tags(xt)
            sc = 2 * len(le & xe) + len(lk & ko_nouns(xt))
            if le & xe and sc >= 3:
                scored.append((sc, x))
        # "related" 칸이 있으면 편집국 지정(빈 배열이면 관련 기사 없음), 없으면 자동
        if isinstance(desk.get("related"), list):
            want = [k for k in desk["related"] if isinstance(k, str)]
            pick_rel = sorted([x for x in arts if x.get("key") in want], key=lambda x: want.index(x["key"]))
        else:
            pick_rel = [x for _, x in sorted(scored, key=lambda t: -t[0])]
        lead["related"] = [{"key": x["key"], "head": (dx.get(x["key"]) or {}).get("headline") or x["head"],
                            "url": x["link"], "src": x["source"]} for x in pick_rel[:3]]

    def card(ai, n):
        x = arts[ai]
        return {"key": x.get("key"), "head": x["head"], "sub": x["sub"], "en": x["en"],
                "deck": trim(x["summary"], n),
                "source": x["source"], "url": x["link"], "badge": x["badge"], "echo": echo(ai),
                "img": x.get("img")}

    # ── 좌측 레일: 외신 TOP 나머지 ──
    left = [card(ai, 150) for ai, x in enumerate(arts) if x["sec"] == "TOP" and ai not in used_art]
    used_art |= {ai for ai, x in enumerate(arts) if x["sec"] == "TOP"}

    # ── 외신면: 나머지 전부를 섹션별로 ──
    foreign = []
    for ai, x in enumerate(arts):
        if ai in used_art:
            continue
        if not foreign or foreign[-1]["name"] != x["sec"]:
            foreign.append({"name": x["sec"], "items": []})
        foreign[-1]["items"].append(card(ai, 120))

    applied = 0
    for it in ([lead] if lead else []) + left + [i for sec in foreign for i in sec["items"]]:
        d = dx.get(it.get("key") or "")
        if not d:
            continue
        hk = "headline" if "headline" in it else "head"
        if d.get("headline"):
            it[hk] = d["headline"]
        if "sub" in d:
            it["sub"] = d["sub"] or ""
        if d.get("drop_echo"):
            # 규칙이 다른 사건을 짝지었다 — 반향을 떼고, 그 글은 다른 칸에 다시 쓸 수 있게 풀어준다
            if it is lead and lead.get("reaction"):
                used_sig.discard(lead["reaction"]["title"])
                lead.update(reaction=None, why=None, kind="desk")
            elif it.get("echo"):
                used_sig.discard(it["echo"]["title"])
                it["echo"] = None
        if d.get("drop_photo"):
            it["img"] = None
            it["no_photo"] = it is not lead      # 톱은 자료사진으로 메우고, 나머지는 글만
        applied += 1

    # 섹션마다 사진 리듬을 준다 — 가디언·한겨레 섹션면처럼 크기를 섞는다.
    #   첫 기사: 16:9 사진 / 4건 이상인 섹션의 셋째: 작은 정사각 썸네일 / 나머지: 글만
    pool = photo_pool(B)
    # 톱에 사진이 없으면(브리핑이 안 달았거나 편집국이 뺐거나) 검수된 자료사진을 붙인다 — 신문 톱은 사진이 있다
    if lead and lead.get("key") and not lead.get("img") and not (dx.get(lead["key"]) or {}).get("no_photo"):
        ph = pick_photo(pool, {"head": lead["headline"], "sub": lead.get("sub", ""), "en": "",
                               "deck": lead.get("deck", "")}, "AI/기술", set())
        if ph:
            src = ph["url"] + "?w=1&h=1&fit=crop&auto=format&q=80"
            lead["img"] = {"src": photo(src, 1200, 675), "sm": photo(src, 640, 360), "alt": ph["alt"],
                           "credit": ph["credit"], "credit_url": ph["credit_url"], "file": True}
    used_ph = {re.sub(r"\?.*$", "", x["img"]["src"]) for x in [lead or {}] + left if x.get("img")}

    # 외신이 다루지 않은 해외 반향 — 규모 상위 (요약 있는 것만)
    wire = [{"key": g.get("discussion") or g.get("url"), "title": g["title"], "url": g.get("discussion") or g.get("url"),
             "deck": trim(g.get("summary_ko"), 90), "comments": g["comments"], "points": g["points"], "r": g["r"]}
            for g in sorted(sig, key=lambda x: -x["comments"])
            if g["title"] not in used_sig and g.get("summary_ko")][:3]
    for w in wire:
        used_sig.add(w["title"])
        d = dx.get(w["key"] or "")
        if d and d.get("headline"):
            w["title_ko"] = d["headline"]
            applied += 1

    # ── 하단 피처: Signal 논쟁 상위 (요약 있는 것 우선) ──
    feats = sorted([g for g in sig if g["title"] not in used_sig and g.get("summary_ko")],
                   key=lambda x: (-x["r"], -x["comments"]))[:2]
    features = [{
        "key": g.get("discussion") or g.get("url"),
        "title": g["title"], "url": g.get("discussion") or g.get("url"),
        "deck": trim(g.get("summary_ko"), 140), "comments": g["comments"],
        "points": g["points"], "r": g["r"], "sides": sides(g.get("summary_ko")),
    } for g in feats]
    for f in features:
        d = dx.get(f["key"] or "")
        if d and d.get("headline"):
            f["title_ko"] = d["headline"]
            applied += 1

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
    marts = (M.get("articles") or [])[:6]
    # 국내 톱 — 기본은 Must News 1위. 매체 수 최다로 고르면 보도자료(기관 발표·협약·출시)가
    # 올라온다(10-06 누리호 이송 88곳). 편집국이 desk.json 의 domestic_lead(Must News 순서)로
    # 가치 있는 기사를 지정하면 그걸 쓴다.
    allm = M.get("articles") or []
    pick = desk.get("domestic_lead")
    dom_pick = isinstance(pick, int) and 0 <= pick < len(allm) and allm[pick] in marts
    dom_lead = (allm[pick] if dom_pick else marts[0]) if marts else None
    domestic = {
        "updated": M.get("updated", ""),
        "picked": bool(dom_pick), "pick_note": desk.get("domestic_note", "") if dom_pick else "",
        "lead": {"title": dom_lead["title"], "url": dom_lead.get("url"),
                 "outlets": dom_lead.get("outlets", 0),
                 "source": dom_lead.get("source", ""),
                 "published": dom_lead.get("publishedAt", ""),
                 # 첫머리는 sharktalk.co.kr 포털이 원문의 og:description 을 받아 준다 — 회차·순서로 부른다
                 "must_id": re.sub(r"\D", "", (M.get("updated") or ""))[:8] + "_" + re.sub(r"\D", "", (M.get("updated") or ""))[8:12],
                 "must_idx": (M.get("articles") or []).index(dom_lead),
                 # 첫 문단 — Must News 가 내보내기 시작하면 바로 쓴다 (칸 이름은 아직 미정이라 후보를 다 본다)
                 "lede": trim(next((dom_lead.get(k) for k in ("lede", "lead", "desc", "description", "summary", "snippet")
                                    if isinstance(dom_lead.get(k), str) and dom_lead.get(k).strip()), ""), 200),
                 "sources": (dom_lead.get("sources") or [])[:12],
                 "angles": angles(dom_lead)} if dom_lead else None,
        "list": [{"rank": a.get("rank"), "title": a["title"], "url": a.get("url"),
                  "outlets": a.get("outlets", 0), "source": a.get("source", ""),
                  "must_idx": (M.get("articles") or []).index(a)}
                 for a in marts if not dom_lead or a["title"] != dom_lead["title"]][:5],
    }

    # 국내 톱 사진 — 검수된 자료사진에서 주제로. 1면 줄에 같은 기사가 오르면 같은 사진을 쓴다.
    if domestic["lead"]:
        ph = pick_photo(pool, {"head": domestic["lead"]["title"], "sub": "", "en": "", "deck": ""},
                        "반도체/인프라", used_ph)
        if ph:
            src = ph["url"] + "?w=1&h=1&fit=crop&auto=format&q=75"
            domestic["lead"]["img"] = {"src": photo(src, 960, 540), "sm": photo(src, 480, 270),
                                       "alt": ph["alt"], "file": True}

    # ── 1면 배치 ──
    # 신문 1면 블록: 톱 + 사이드 2~3건 + 하단 주요기사 줄 3~4건. 외신·국내·커뮤니티를 섞는다.
    # 편집국이 desk.json "front": {"side": [키…], "row": [키…]} 로 고르면 그대로, 없으면 기본 배치.
    # 키: 외신 "<브리핑날짜>#art-N", 국내 "must:<Must News 순서>", 커뮤니티 = 토론 URL.
    # 1면에 올린 기사는 아래 섹션면에서 뺀다 (같은 기사가 두 번 나오지 않게).
    def short_src(src):
        t = re.sub(r"다매체 보도|Featured|Hero", "", src or "").strip()
        ps = [x.strip() for x in t.split(",") if x.strip()]
        return ("%s 외 %d곳" % (ps[0], len(ps) - 1)) if len(ps) > 1 else (ps[0] if ps else "")

    def plat(u):
        return "Reddit" if "reddit.com" in (u or "") else "Hacker News"

    cand = {}
    for it in left:
        cand[it["key"]] = {"key": it["key"], "kind": "foreign", "label": "외신", "head": it["head"],
                           "sub": it["sub"], "deck": it["deck"], "img": it.get("img"), "url": it["url"],
                           "src": short_src(it["source"]), "echo": it.get("echo")}
    for sec in foreign:
        for it in sec["items"]:
            cand[it["key"]] = {"key": it["key"], "kind": "foreign",
                               "label": "외신 · " + ("Pick" if "Pick" in sec["name"] else sec["name"]),
                               "head": it["head"], "sub": it["sub"], "deck": it["deck"], "img": None,
                               "no_photo": it.get("no_photo"), "url": it["url"],
                               "src": short_src(it["source"]), "echo": it.get("echo"), "sec": sec["name"]}
    must_order = M.get("articles") or []
    for a in marts:
        k = "must:%d" % must_order.index(a)
        d = dx.get(k) or {}
        if d.get("headline"):
            applied += 1
        cand[k] = {"key": k, "kind": "domestic", "label": "국내", "head": d.get("headline") or a["title"],
                   "sub": d.get("sub", ""), "deck": "",
                   "img": (domestic["lead"] or {}).get("img") if a is dom_lead else None, "url": a.get("url"),
                   "src": "%s · %s곳 보도" % (a.get("source", ""), a.get("outlets", 0)),
                   "must_idx": must_order.index(a)}
    for f in features + wire:
        cand[f["key"]] = {"key": f["key"], "kind": "community", "label": "커뮤니티 · " + plat(f["url"]),
                          "head": f.get("title_ko") or f["title"], "sub": f["title"] if f.get("title_ko") else "",
                          "deck": f["deck"], "img": None, "url": f["url"],
                          "src": plat(f["url"]) + " · 댓글 {:,}".format(f["comments"]) +
                                 (" · 추천 {:,}".format(f["points"]) if f.get("points") else "")}

    dom_key = "must:%d" % must_order.index(dom_lead) if dom_lead else None
    side_def = [it["key"] for it in left][:3]
    row_def = [k for k in ([dom_key] + [f["key"] for f in features[:1]] + [w["key"] for w in wire[:1]] +
                           [sec["items"][0]["key"] for sec in foreign[:1] if sec["items"]]) if k]
    fr = desk.get("front") or {}
    side = [k for k in (fr.get("side") or []) if k in cand][:3]
    picked_front = len(side) >= 2
    if not picked_front:
        side = side_def
    row = [k for k in (fr.get("row") or []) if k in cand and k not in side][:4]
    if len(row) < 3:
        row = [k for k in row_def if k not in side][:4]
    else:
        picked_front = True

    # 1면 기사에 사진 — 외신 TOP 은 브리핑 사진, 나머지는 검수된 자료사진에서 주제로
    for k in side + row:
        it = cand[k]
        if it.get("img") or it.get("no_photo") or (dx.get(k) or {}).get("drop_photo"):
            continue
        ph = pick_photo(pool, {"head": it["head"], "sub": it["sub"], "en": "", "deck": it["deck"]},
                        it.get("sec") or ("AI/기술" if it["kind"] != "domestic" else "반도체/인프라"), used_ph)
        if ph:
            src = ph["url"] + "?w=1&h=1&fit=crop&auto=format&q=75"
            it["img"] = {"src": photo(src, 960, 540), "sm": photo(src, 480, 270), "alt": ph["alt"], "file": True}

    placed = set(side + row)
    rest_top = [it for it in left if it["key"] not in placed]
    foreign = [{"name": sc["name"], "items": [i for i in sc["items"] if i["key"] not in placed]} for sc in foreign]
    if rest_top:
        foreign.insert(0, {"name": "TOP", "items": rest_top})
    foreign = [sc for sc in foreign if sc["items"]]
    features = [f for f in features if f["key"] not in placed]
    wire = [w for w in wire if w["key"] not in placed]
    slots = {"side": [cand[k] for k in side], "row": [cand[k] for k in row], "picked": picked_front,
             "must_id": (lambda u: u[:8] + "_" + u[8:12] if len(u) >= 12 else "")(re.sub(r"\D", "", M.get("updated") or ""))}

    # 외신면 사진 리듬 — 섹션 첫 기사 16:9, 4건 이상 섹션의 셋째 정사각 썸네일
    for sec in foreign:
        for i, it in enumerate(sec["items"]):
            if it.get("img") and not it["img"].get("file"):
                continue                       # 브리핑이 고른 사진(TOP)은 그대로
            it["img"] = None
            size = "lead" if i == 0 else ("side" if i == 2 and len(sec["items"]) >= 4 else None)
            if not size or it.get("no_photo"):
                continue
            ph = pick_photo(pool, it, sec["name"], used_ph)
            if ph:
                w, h = (960, 540) if size == "lead" else (240, 240)
                it["img"] = {"src": photo(ph["url"] + "?w=1&h=1&fit=crop&auto=format&q=75", w, h),
                             "alt": ph["alt"], "credit": ph["credit"], "credit_url": ph["credit_url"],
                             "size": size, "file": True}

    # ── 해석 레일: 패널 + 신뢰층 ──
    voices = []
    for a in sorted(panel.get("accounts") or [], key=lambda x: x.get("tier", "C")):
        p = next((q for q in (a.get("posts") or []) if PANEL_TOPIC.search(q.get("t", ""))), {})
        if p.get("t"):
            voices.append({"kind": "panel", "who": a.get("n"), "handle": a.get("h"),
                           "tier": a.get("tier", "C"), "role": a.get("role", ""),
                           "text": trim(p["t"], 115), "full": p["t"], "ts": p.get("ts"),
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

    # ── 기업 뉴스룸 공식 사진 — 회사가 주인공인 기사엔 Unsplash 대신 그 회사 사진 ('<회사> 제공') ──
    # 편집국이 drop_photo·no_photo 로 막은 기사는 건드리지 않는다. 같은 사진은 한 면에 한 번.
    nrp, nr_used = newsroom_pool(), set()
    def nr_swap(it, title, sub="", deck="", key=None):
        if not it or (dx.get(key) or {}).get("drop_photo") or (dx.get(key) or {}).get("no_photo") or it.get("no_photo"):
            return
        ph = pick_newsroom(nrp, {"title": " ".join((title or "", sub or "")), "deck": deck or ""}, nr_used)
        if ph:
            size = (it.get("img") or {}).get("size")
            it["img"] = dict(ph, **({"size": size} if size else {}))
    if lead:
        nr_swap(lead, lead.get("headline"), lead.get("sub"), lead.get("deck"), lead.get("key"))
    for it in (slots.get("side") or []) + (slots.get("row") or []):
        nr_swap(it, it.get("head"), it.get("sub"), it.get("deck"), it.get("key"))
    if domestic.get("lead"):
        nr_swap(domestic["lead"], domestic["lead"].get("title"), "", domestic["lead"].get("lede"), None)
    for sec in foreign:
        for it in sec["items"]:
            if it.get("img"):
                nr_swap(it, it.get("head"), it.get("sub"), it.get("deck"), it.get("key"))

    # ── 주요 경제 일정 — 경제 일정 루틴(06:20)이 쓴 data/calendar.json 에서 오늘~2일 뒤(3일치) ──
    # 확정(verified)된 것만 싣는다. 편집국이 desk.json "calendar_drop": [제목…] 으로 뺄 수 있다.
    cal = load(os.path.join(DATA, "calendar.json"), {}) or {}
    d0 = now.date().isoformat()
    d2 = (now.date() + timedelta(days=2)).isoformat()
    cal_drop = {str(t).strip() for t in (desk.get("calendar_drop") or [])}
    cal_ev = [e for e in (cal.get("events") or [])
              if isinstance(e, dict) and d0 <= str(e.get("date", "")) <= d2 and e.get("title")
              and e.get("verified") is True and int(e.get("importance") or 0) >= 3
              and str(e["title"]).strip() not in cal_drop]
    cal_ev.sort(key=lambda e: (e["date"], e.get("time") or "99:99"))
    calendar = {"generated_at": cal.get("generated_at"), "from": d0, "to": d2, "dropped": len(cal_drop),
                "events": [{k: e.get(k) for k in ("date", "time", "region", "category", "title", "importance",
                                                   "why", "consensus", "previous", "source", "verified")}
                           for e in cal_ev]} if cal_ev else None

    out = {
        "edition": now.strftime("%Y-%m-%d %H:%M"),
        "no": no,
        "sources": {"briefing": brief.get("date", ""), "must": M.get("updated", ""),
                    "signal": latest.get("updated", "")},
        "links": {"briefing": blink, "must": LINK_MUST},
        "desk": {"edited_at": desk.get("edited_at"), "applied": applied,
                 "notes": desk.get("notes") or []},
        "lead": lead, "slots": slots, "left": [], "foreign": foreign, "wire": wire, "features": features,
        "temperature": temp,
        "domestic": domestic, "voices": voices, "calendar": calendar,
    }
    io.open(os.path.join(DATA, "front.json"), "w", encoding="utf-8").write(
        json.dumps(out, ensure_ascii=False, indent=1))

    print("1면 조판 — %s" % out["edition"])
    if lead:
        print("  톱(%s): %s" % (lead["kind"], lead["headline"][:50]))
        if lead.get("why"):
            print("     교차 근거: 개체 %s · 공유 %s" % (lead["why"]["entities"], lead["why"]["shared"]))
    print("  1면 사이드 %s · 줄 %s%s" % ([x["kind"][0] for x in slots["side"]], [x["kind"][0] for x in slots["row"]],
                                     " (편집국 배치)" if slots["picked"] else ""))
    print("  외신 %d건(좌측 %d · 외신면 %d) · 반향 붙음 %d · 해외 %d · 피처 %d · 온도 %s · 국내 %d · 해석 %d" % (
        len(arts), len(left), sum(len(f["items"]) for f in foreign), len(echo_of), len(wire), len(features), temp["group"] if temp else "-",
        len(domestic["list"]) + (1 if domestic["lead"] else 0), len(voices)))


if __name__ == "__main__":
    main()
