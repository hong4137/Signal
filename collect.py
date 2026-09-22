#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Signal Layer 수집기 — buzz-first

설계 근거는 RESUME_SIGNAL_LAYER_20260914.md 참조.
핵심 원칙: 유형을 분류하지 않는다. "커뮤니티가 시끄러웠다"는 사실만 잡는다.

출력
  data/latest.json                  최신 판
  data/archive/YYYYMMDD_HHMM.json   판본 스냅샷
  data/archive/index.json           판본 목록
  data/trend.json                   주제군 14일 추세 (배경 지표)

사용
  python collect.py                 기본 (60일 윈도우, 댓글 200+)
  python collect.py --days 7        최근 7일만
  python collect.py --no-trend      추세 계산 생략 (HTTP 14회 절약)
  python collect.py --dry-run       파일 쓰지 않고 표만 출력
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
KST = timezone(timedelta(hours=9))

# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────

# AI/테크 도메인 게이트. 이것 없이 r로 정렬하면 미국 정치·사회 이슈가 잠식한다
# (실측: 공중화장실 r=2.38, 캐나다 관세 1698댓글, 후티 반군, 화성 초코바...)
GATE = re.compile(
    r"\bai\b|\ba\.i\.|llm|gpt|claude|anthropic|openai|gemini|deepseek|qwen|llama|mistral|"
    r"model|agent|neural|nvidia|gpu|chip|semiconduct|tsmc|data ?cent|training|inference|"
    r"open-?weight|copilot|prompt|machine learning|transformer|diffusion|robot|autonom|"
    r"hugging ?face|pytorch|cuda|tokeniz|fine-?tun|rag\b|embedding",
    re.I,
)

# 주제군 — 게이트 통과분을 다시 나눠 추세를 본다 (배경 지표)
GROUPS = {
    "안전·거버넌스": r"slow down|pace the|alignment|misalign|extinction|ai safety|regulat|"
                     r"doom|superintell|open the weights|frontier|existential|moratorium",
    "모델릴리스":   r"\bgpt-|claude|gemini|llama|deepseek|qwen|mistral|opus|sonnet|"
                     r"open-?weight|\bllm\b|releases?\b|launch",
    "에이전트":     r"\bagents?\b|agentic|autonomous|tool use|mcp\b",
    "인프라·칩":    r"nvidia|gpu|data ?cent|tpu|chip|semiconduct|tsmc|compute|inference cost|"
                     r"h100|b200|cuda",
    "보안·사고":    r"attack|breach|vulnerab|exploit|malware|supply chain|backdoor|leak|"
                     r"prompt injection|jailbreak",
    "개발도구":     r"rust|python|compiler|database|kubernetes|homebrew|linux|\bgit\b|"
                     r"framework|pandas|typescript|copilot|ide\b",
}
GROUPS_RE = {k: re.compile(v, re.I) for k, v in GROUPS.items()}

# GitHub 노이즈 차단 — 실측 노이즈: IDM 크랙툴, 영상생성기, text-humanizer
GH_SPAM = re.compile(
    r"crack|激活|activation|keygen|patcher|humaniz|free ?download|nulled|"
    r"短视频|video ?generator|tiktok ?bot|follower|\bvpn\b|proxy ?pool",
    re.I,
)

# 신뢰층 — 해석·프레이밍 제공. 반응 수치는 없으나 '누가 다뤘나'가 신호
TRUST_FEEDS = [
    ("Simon Willison",      "https://simonwillison.net/atom/everything/"),
    ("Latent Space",        "https://www.latent.space/feed"),
    ("Zvi (DWATV)",         "https://thezvi.substack.com/feed"),
    ("AI Supremacy",        "https://aisupremacy.substack.com/feed"),
    ("Deep Learning Focus", "https://cameronrwolfe.substack.com/feed"),
]

UA = "Mozilla/5.0 (compatible; SignalLayer/1.0; +https://github.com/hong4137)"


# ─────────────────────────────────────────────────────────────
# utils
# ─────────────────────────────────────────────────────────────

def _get(url, timeout=30, headers=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def get_json(url, timeout=30, headers=None):
    return json.loads(_get(url, timeout, headers))


def story_key(url):
    """층이 다른 기록을 같은 사건으로 묶는 키.

    나중에 넣으면 전 층 재처리이므로 처음부터 찍는다.
    """
    if not url:
        return None
    u = url.strip()
    m = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5})", u, re.I)
    if m:
        return "arxiv:" + m.group(1)
    m = re.search(r"github\.com/([^/\s]+)/([^/\s#?]+)", u, re.I)
    if m:
        return "gh:%s/%s" % (m.group(1).lower(), m.group(2).lower().removesuffix(".git"))
    m = re.search(r"huggingface\.co/([^/\s]+)/([^/\s#?]+)", u, re.I)
    if m:
        return "hf:%s/%s" % (m.group(1).lower(), m.group(2).lower())
    # ⚠️ 식별자가 쿼리에만 있는 사이트는 특수 처리해야 한다.
    #    경로만 쓰면 HN 스레드 전체가 url:news.ycombinator.com/item 하나로 뭉개져
    #    신뢰층이 아무 HN 링크나 달아도 전 항목에 매칭된다(실측 사고).
    m = re.search(r"news\.ycombinator\.com/item\?id=(\d+)", u, re.I)
    if m:
        return "hn:" + m.group(1)
    m = re.search(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{6,})", u, re.I)
    if m:
        return "yt:" + m.group(1)
    try:
        p = urllib.parse.urlsplit(u)
        host = (p.netloc or "").lower().removeprefix("www.")
        path = (p.path or "/").rstrip("/") or "/"
        keep = {"id", "v", "p", "postid", "story", "article", "idxno", "no", "seq", "aid"}
        qs = sorted((k.lower(), vv) for k, vv in urllib.parse.parse_qsl(p.query)
                    if k.lower() in keep and vv)
        tail = ("?" + "&".join("%s=%s" % kv for kv in qs)) if qs else ""
        return "url:%s%s%s" % (host, path, tail)
    except Exception:
        return "url:" + u[:120]


def topics_of(text):
    return [g for g, rx in GROUPS_RE.items() if rx.search(text or "")]


def passes_gate(text):
    return bool(GATE.search(text or ""))


# ─────────────────────────────────────────────────────────────
# collectors
# ─────────────────────────────────────────────────────────────

def collect_hn(days, min_comments):
    """buzz 본체. 댓글수=참여규모, r=댓글÷업보트=논쟁성격."""
    since = int(time.time()) - days * 86400
    rows, seen_id = [], set()
    for page in range(6):
        url = (
            "https://hn.algolia.com/api/v1/search?tags=story"
            "&numericFilters=created_at_i>%d,num_comments>%d"
            "&hitsPerPage=100&page=%d" % (since, min_comments, page)
        )
        try:
            d = get_json(url)
        except Exception as e:
            print("  [HN] 오류 p%d: %s" % (page, e), file=sys.stderr)
            break
        for h in d.get("hits", []):
            if h.get("objectID") in seen_id:
                continue
            seen_id.add(h.get("objectID"))
            rows.append(h)
        if page >= d.get("nbPages", 1) - 1:
            break

    out, seen_title = [], set()
    for h in rows:
        title = (h.get("title") or "").strip()
        if not title or title.lower() in seen_title:
            continue
        if not passes_gate(title):
            continue
        seen_title.add(title.lower())
        pts = h.get("points") or 0
        cmts = h.get("num_comments") or 0
        hn_url = "https://news.ycombinator.com/item?id=%s" % h.get("objectID")
        out.append({
            "layer": "hn",
            "title": title,
            "url": h.get("url") or hn_url,
            "discussion": hn_url,
            "points": pts,
            "comments": cmts,
            "r": round(cmts / max(pts, 1), 2),
            "at": (h.get("created_at") or "")[:10],
            "topics": topics_of(title),
            "story_key": story_key(h.get("url") or hn_url),
        })
    return out


def collect_hf(limit=15):
    """모델 트렌딩 = 기술 트렌드 그 자체. HN이 놓치는 걸 잡는다
    (실측: HN 모델릴리스 0인 날 HF에선 DeepSeek V4.1-Flash 1위)."""
    try:
        ms = get_json(
            "https://huggingface.co/api/models?sort=trendingScore&direction=-1&limit=%d" % limit
        )
    except Exception as e:
        print("  [HF] 오류: %s" % e, file=sys.stderr)
        return []
    out = []
    for m in ms:
        mid = m.get("id") or ""
        out.append({
            "layer": "hf",
            "title": mid,
            "url": "https://huggingface.co/" + mid,
            "trending": round(m.get("trendingScore") or 0),
            "likes": m.get("likes") or 0,
            "downloads": m.get("downloads") or 0,
            "topics": topics_of(mid) or ["모델릴리스"],
            "story_key": "hf:" + mid.lower(),
        })
    return out


def collect_github(days=7, min_stars=200, limit=20):
    """신규 프로젝트 등장 탐지 전용. 게이트 + 스팸 차단 둘 다 건다."""
    d = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    url = (
        "https://api.github.com/search/repositories?q=created:>%s+stars:>%d"
        "&sort=stars&order=desc&per_page=%d" % (d, min_stars, limit)
    )
    hdr = {}
    if os.environ.get("GITHUB_TOKEN"):
        hdr["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
    try:
        items = get_json(url, headers=hdr).get("items", [])
    except Exception as e:
        print("  [GH] 오류: %s" % e, file=sys.stderr)
        return []
    out = []
    for r in items:
        name = r.get("full_name") or ""
        desc = r.get("description") or ""
        blob = name + " " + desc
        if GH_SPAM.search(blob):
            continue
        if not passes_gate(blob):
            continue
        out.append({
            "layer": "github",
            "title": name,
            "desc": desc[:160],
            "url": r.get("html_url"),
            "stars": r.get("stargazers_count") or 0,
            "topics": topics_of(blob),
            "story_key": "gh:" + name.lower(),
        })
    return out


def _feed_items(xml, n=6):
    items = re.findall(r"<item>(.*?)</item>", xml, re.S)
    if not items:
        items = re.findall(r"<entry>(.*?)</entry>", xml, re.S)
    out = []
    for it in items[:n]:
        t = re.search(r"<title[^>]*>\s*(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?\s*</title>", it, re.S)
        l = re.search(r'<link[^>]*href="([^"]+)"', it) or re.search(r"<link>(.*?)</link>", it, re.S)
        dt = (re.search(r"<pubDate>(.*?)</pubDate>", it, re.S)
              or re.search(r"<updated>(.*?)</updated>", it, re.S))
        title = re.sub(r"<[^>]+>", "", t.group(1)).strip() if t else ""
        for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&#39;", "'"), ("&quot;", '"')):
            title = title.replace(a, b)

        # 본문 안 링크가 매칭의 핵심이다.
        # 신뢰층 글의 자기 URL은 buzz 항목의 원문 URL과 절대 안 맞는다.
        # Simon Willison이 RubyGems 사건을 쓰면 본문에 원문을 링크하므로,
        # 거기서 story_key를 뽑아야 "누가 이 사건을 다뤘나"가 붙는다.
        body = ""
        for tag in ("content:encoded", "description", "content", "summary"):
            m = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), it, re.S)
            if m:
                body += m.group(1)
        links = re.findall(r'href=[\'"]?(https?://[^\'" <>]+)', body)
        self_host = urllib.parse.urlsplit(l.group(1) if l else "").netloc.lower()
        keys = set()
        for u in links[:60]:
            host = urllib.parse.urlsplit(u).netloc.lower()
            # 자기 사이트·소셜·구독 링크는 버린다
            if host == self_host or re.search(
                r"substack\.com/(subscribe|profile|account)|twitter\.com|x\.com|"
                r"facebook\.com|linkedin\.com|mailto|/cdn-cgi/", u, re.I):
                continue
            k = story_key(u)
            if k:
                keys.add(k)

        out.append({
            "title": title,
            "url": (l.group(1).strip() if l else ""),
            "at": (dt.group(1).strip()[:16] if dt else ""),
            "link_keys": sorted(keys)[:40],
        })
    return out


def collect_trust():
    """신뢰층. 반응 수치가 없으므로 '누가 다뤘나'만 기록하고
    나중에 story_key로 buzz 항목에 붙인다."""
    out = []
    for name, url in TRUST_FEEDS:
        try:
            xml = _get(url, timeout=25)
        except Exception as e:
            print("  [신뢰층] %s 오류: %s" % (name, e), file=sys.stderr)
            continue
        for it in _feed_items(xml, n=20):   # 14일 윈도우를 덮으려면 6개로는 모자람
            if not it["title"]:
                continue
            out.append({
                "layer": "trust",
                "source": name,
                "title": it["title"],
                "url": it["url"],
                "at": it["at"],
                "topics": topics_of(it["title"]),
                "story_key": story_key(it["url"]),
                "link_keys": it.get("link_keys", []),
            })
    return out


REDDIT_SUBS = ("LocalLLaMA", "MachineLearning", "singularity")
REDDIT_UA = "windows:jfnb-signal:v1.0 (by /u/Electronic_Fudge2550)"


def collect_reddit(min_comments=150, days=3):
    """r/LocalLLaMA 등 — '신기한 AI 행동' 경험담의 본진.

    경로가 둘이다.
      · OAuth  — REDDIT_CLIENT_ID/SECRET 이 있으면 점수·댓글수까지 받는다 (상위 호환)
      · RSS    — 기본값. 등록 불필요. 단 점수·댓글수가 없어 순위를 대용으로 쓴다

    ⚠️ 공개 .json 은 UA 와 무관하게 403 이다. .rss 만 열린다.
       그리고 반드시 '고유한' UA 를 보내야 한다 — Mozilla/5.0 같은 일반 UA 는 403.
    """
    oauth = _reddit_oauth(min_comments)
    if oauth:
        return oauth
    return _reddit_rss(days)


def _reddit_rss(days=3):
    """RSS 경로. /top/?t=… 는 Reddit 이 매긴 인기순이므로 순위 자체가 buzz 신호다
    (Threads 의 search_type=TOP 을 쓰기로 한 것과 같은 논리)."""
    t = "day" if days <= 3 else ("week" if days <= 10 else "month")
    out = []
    for si, sub in enumerate(REDDIT_SUBS):
        if si:
            time.sleep(5)  # 레이트리밋이 빡빡하다. 하루 몇 회면 충분하지만 연타는 429
        xml = None
        for attempt in range(3):          # 429 는 잠깐 기다리면 대개 풀린다
            try:
                xml = _get("https://www.reddit.com/r/%s/top/.rss?t=%s" % (sub, t),
                           timeout=25, headers={"User-Agent": REDDIT_UA})
                break
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < 2:
                    time.sleep(12 * (attempt + 1))
                    continue
                print("  [Reddit] r/%s RSS 오류: %s" % (sub, e), file=sys.stderr)
                break
            except Exception as e:
                print("  [Reddit] r/%s RSS 오류: %s" % (sub, e), file=sys.stderr)
                break
        if not xml:
            continue
        entries = re.findall(r"<entry>(.*?)</entry>", xml, re.S)
        for rank, e in enumerate(entries[:15], 1):
            m = re.search(r"<title>(.*?)</title>", e, re.S)
            if not m:
                continue
            title = re.sub(r"<[^>]+>", "", m.group(1)).strip()
            for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&#39;", "'"), ("&quot;", '"')):
                title = title.replace(a, b)
            lm = re.search(r'<link[^>]*href="([^"]+)"', e)
            perma = lm.group(1) if lm else ""
            # content 안의 바깥 링크가 있으면 그게 원문 — story_key 로 층간 연결된다
            body = re.search(r"<content[^>]*>(.*?)</content>", e, re.S)
            ext = ""
            if body:
                for u in re.findall(r"href=&quot;(https?://[^&]+)&quot;", body.group(1)):
                    if "reddit.com" not in u and "redd.it" not in u:
                        ext = u.replace("&amp;", "&")
                        break
            out.append({
                "layer": "reddit",
                "sub": sub,
                "title": title,
                "url": ext or perma,
                "discussion": perma,
                "rank": rank,
                "points": 0,          # RSS 는 수치를 주지 않는다
                "comments": 0,
                "r": 0.0,
                "has_metrics": False,
                "topics": topics_of(title),
                "story_key": story_key(ext or perma),
            })
    return out


def _reddit_oauth(min_comments=150):
    cid = os.environ.get("REDDIT_CLIENT_ID")
    sec = os.environ.get("REDDIT_CLIENT_SECRET")
    if not (cid and sec):
        return []
    import base64
    try:
        auth = base64.b64encode(("%s:%s" % (cid, sec)).encode()).decode()
        req = urllib.request.Request(
            "https://www.reddit.com/api/v1/access_token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": "Basic " + auth, "User-Agent": UA},
        )
        with urllib.request.urlopen(req, timeout=25) as r:
            token = json.loads(r.read().decode())["access_token"]
    except Exception as e:
        print("  [Reddit] 토큰 실패: %s" % e, file=sys.stderr)
        return []

    out = []
    for sub in REDDIT_SUBS:
        try:
            d = get_json(
                "https://oauth.reddit.com/r/%s/top?t=week&limit=50" % sub,
                headers={"Authorization": "Bearer " + token, "User-Agent": REDDIT_UA},
            )
        except Exception as e:
            print("  [Reddit] r/%s 오류: %s" % (sub, e), file=sys.stderr)
            continue
        for c in d.get("data", {}).get("children", []):
            p = c.get("data", {})
            score = p.get("score") or 0
            cmts = p.get("num_comments") or 0
            if cmts < min_comments:
                continue
            title = p.get("title") or ""
            if not passes_gate(title) and sub == "singularity":
                continue
            link = "https://reddit.com" + (p.get("permalink") or "")
            out.append({
                "layer": "reddit",
                "sub": sub,
                "title": title,
                "url": p.get("url_overridden_by_dest") or link,
                "discussion": link,
                "points": score,
                "comments": cmts,
                "r": round(cmts / max(score, 1), 2),
                "has_metrics": True,
                "topics": topics_of(title),
                "story_key": story_key(p.get("url_overridden_by_dest") or link),
            })
    return out


# ─────────────────────────────────────────────────────────────
# 주제군 추세 (배경 지표)
# ─────────────────────────────────────────────────────────────

def collect_trend(days=14, min_points=100):
    now = int(time.time())
    series, totals = [], {g: 0 for g in GROUPS}
    for i in range(days - 1, -1, -1):
        a, b = now - (i + 1) * 86400, now - i * 86400
        url = ("https://hn.algolia.com/api/v1/search?tags=story"
               "&numericFilters=created_at_i>%d,created_at_i<%d,points>%d"
               "&hitsPerPage=100" % (a, b, min_points))
        try:
            hits = get_json(url).get("hits", [])
        except Exception:
            hits = []
        day = {g: 0 for g in GROUPS}
        for h in hits:
            t = (h.get("title") or "")
            p = h.get("points") or 0
            for g in topics_of(t):
                day[g] += p
                totals[g] += p
        series.append({
            "date": datetime.fromtimestamp(a, KST).strftime("%m-%d"),
            "n": len(hits),
            **day,
        })
    return {"series": series, "totals": totals, "days": days, "min_points": min_points}


# ─────────────────────────────────────────────────────────────
# 조립
# ─────────────────────────────────────────────────────────────

def attach_trust(buzz, trust):
    """신뢰층이 이 사건을 다뤘는지 붙인다.
    '숫자는 작은데 신뢰층이 다룸' = 조용한 중요 사건.

    매칭 우선순위
      1) 신뢰층 글 본문이 이 URL을 링크함  (가장 강함)
      2) story_key 동일                     (같은 원문을 가리킴)
      3) 제목 고유명사 3개 이상 겹침         (최후 수단, 오탐 방지로 문턱 높임)
    """
    # 묶음 뉴스레터(Latent Space [AINews] 등)는 수십 개를 링크하므로
    # 링크 매칭에서 제외한다. 안 그러면 전 항목에 붙어 신호가 사라진다.
    ROUNDUP_LINKS = 25
    link_idx, key_idx = {}, {}
    for t in trust:
        lk = t.get("link_keys", [])
        t["is_roundup"] = len(lk) > ROUNDUP_LINKS
        if not t["is_roundup"]:
            for k in lk:
                link_idx.setdefault(k, []).append(t)
        if t.get("story_key"):
            key_idx.setdefault(t["story_key"], []).append(t)

    STOP = {"that", "with", "from", "this", "what", "when", "your", "have", "will",
            "about", "into", "they", "their", "been", "more", "than", "just", "only",
            "after", "over", "using", "make", "made", "does", "isn", "don", "can"}

    def toks(s):
        return {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9.+-]{3,}", s or "")
                if w.lower() not in STOP}

    for b in buzz:
        hits, how = [], None
        for k in filter(None, [b.get("story_key"), story_key(b.get("discussion"))]):
            if k in link_idx:
                hits += link_idx[k]
                how = "link"
        if not hits and b.get("story_key") in key_idx:
            hits += key_idx[b["story_key"]]
            how = "key"
        if not hits:
            bt = toks(b["title"])
            for t in trust:
                if len(bt & toks(t["title"])) >= 3:
                    hits.append(t)
                    how = "title"
        if hits:
            b["trust"] = sorted({h["source"] for h in hits})
            b["trust_match"] = how
    return buzz


def build(args):
    print("수집 중...", file=sys.stderr)
    hn = collect_hn(args.days, args.min_comments)
    print("  HN       %3d건 (게이트 통과)" % len(hn), file=sys.stderr)
    rd = collect_reddit(days=args.days)
    rd_metric = [x for x in rd if x.get("has_metrics")]
    rd_rank = [x for x in rd if not x.get("has_metrics")]
    print("  Reddit   %3d건 (%s)" % (
        len(rd), "OAuth·수치 있음" if rd_metric else ("RSS·순위만" if rd else "실패")),
        file=sys.stderr)
    hf = collect_hf()
    print("  HF       %3d건" % len(hf), file=sys.stderr)
    gh = collect_github()
    print("  GitHub   %3d건 (스팸·게이트 필터 후)" % len(gh), file=sys.stderr)
    tr = collect_trust()
    print("  신뢰층    %3d건" % len(tr), file=sys.stderr)

    # 수치가 있는 것만 buzz 랭킹에 섞는다. RSS Reddit 은 수치가 없어
    # 같은 표에 넣으면 전부 0 으로 바닥에 깔린다 — 별도 구획으로 뺀다.
    buzz = attach_trust(hn + rd_metric, tr)
    rd_rank = attach_trust(rd_rank, tr)

    # 한 점수로 뭉개지 않는다 — 두 축을 각각 정렬해 병기
    by_volume = sorted(buzz, key=lambda x: -x["comments"])
    by_debate = sorted([b for b in buzz if b["comments"] >= args.min_comments],
                       key=lambda x: (-x["r"], -x["comments"]))
    quiet = sorted([b for b in buzz if b.get("trust")],
                   key=lambda x: (-len(x["trust"]), -x["comments"]))

    now = datetime.now(KST)
    payload = {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "window_days": args.days,
        "min_comments": args.min_comments,
        "counts": {"hn": len(hn), "reddit": len(rd), "hf": len(hf),
                   "github": len(gh), "trust": len(tr)},
        "reddit_mode": "oauth" if rd_metric else ("rss" if rd_rank else "none"),
        "by_volume": by_volume[: args.top],
        "by_debate": by_debate[: args.top],
        "reddit_top": rd_rank[:15],
        "quiet_important": quiet[:10],
        "models": hf[:10],
        "new_repos": gh[:10],
        "trust_recent": tr[:20],
    }
    return payload, now


def write_out(payload, now, trend):
    os.makedirs(os.path.join(DATA, "archive"), exist_ok=True)
    stamp = now.strftime("%Y%m%d_%H%M")

    with open(os.path.join(DATA, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    with open(os.path.join(DATA, "archive", stamp + ".json"), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)

    idx_path = os.path.join(DATA, "archive", "index.json")
    idx = {"updated": payload["updated"], "editions": []}
    if os.path.exists(idx_path):
        try:
            idx = json.load(open(idx_path, encoding="utf-8"))
        except Exception:
            pass
    idx["updated"] = payload["updated"]
    top = payload["by_volume"][0]["title"] if payload["by_volume"] else ""
    idx["editions"] = ([{"stamp": stamp, "n": len(payload["by_volume"]), "top": top[:90]}]
                       + [e for e in idx.get("editions", []) if e.get("stamp") != stamp])[:400]
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(idx, f, ensure_ascii=False, indent=1)

    if trend:
        with open(os.path.join(DATA, "trend.json"), "w", encoding="utf-8") as f:
            json.dump(trend, f, ensure_ascii=False, indent=1)
    return stamp


def report(payload, trend):
    def line(b):
        badge = (" [신뢰층: %s]" % ", ".join(b["trust"])) if b.get("trust") else ""
        lay = {"hn": "HN", "reddit": "RD"}.get(b["layer"], b["layer"])
        return "  %s %5d pt %5d c  r=%.2f  %s%s" % (
            lay, b["points"], b["comments"], b["r"], b["title"][:62], badge)

    print("\n" + "=" * 78)
    print("SIGNAL  %s  (윈도우 %d일)" % (payload["updated"], payload["window_days"]))
    print("=" * 78)
    print("\n■ 참여 규모 (댓글수)")
    for b in payload["by_volume"][:10]:
        print(line(b))
    print("\n■ 논쟁 (r = 댓글÷업보트)")
    for b in payload["by_debate"][:10]:
        print(line(b))
    if payload.get("reddit_top"):
        print("\n■ Reddit (RSS — 수치 없음, Reddit 인기순 그대로)")
        for b in payload["reddit_top"][:8]:
            badge = (" [신뢰층: %s]" % ", ".join(b["trust"])) if b.get("trust") else ""
            print("  #%-2d r/%-16s %s%s" % (b["rank"], b["sub"], b["title"][:52], badge))
    if payload["quiet_important"]:
        print("\n■ 조용한 중요 사건 (신뢰층이 다룸)")
        for b in payload["quiet_important"][:6]:
            print(line(b))
    print("\n■ 모델 트렌딩")
    for m in payload["models"][:5]:
        print("  ts=%-6d likes=%-6d %s" % (m["trending"], m["likes"], m["title"]))
    if payload["new_repos"]:
        print("\n■ 신규 저장소")
        for g in payload["new_repos"][:5]:
            print("  *%-6d %s — %s" % (g["stars"], g["title"], g["desc"][:46]))
    if trend:
        print("\n■ 주제군 추세 (최근 %d일, HN %dpt+)" % (trend["days"], trend["min_points"]))
        days = [s["date"] for s in trend["series"]]
        print("  %-14s%s" % ("", "".join("%7s" % d for d in days[-10:])))
        for g, _ in sorted(trend["totals"].items(), key=lambda kv: -kv[1]):
            print("  %-14s%s" % (g, "".join("%7d" % s[g] for s in trend["series"][-10:])))
    print()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--min-comments", type=int, default=200)
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--no-trend", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    payload, now = build(args)
    trend = None if args.no_trend else collect_trend()
    report(payload, trend)

    if args.dry_run:
        print("(--dry-run: 파일 쓰지 않음)")
        return
    stamp = write_out(payload, now, trend)
    print("저장: data/latest.json · data/archive/%s.json%s" %
          (stamp, " · data/trend.json" if trend else ""))


if __name__ == "__main__":
    main()
