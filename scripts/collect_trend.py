"""트렌드세터 수집기 — 층 1 (자동 수집, LLM 없음). GitHub Actions 3시간마다.

설계: 트렌드세터_LLM위키/wiki/운영/수집-파이프라인.md · 출처: 위키 출처-등록부 '기업 이벤트 출처' 절(2026-10-10 실측).
원칙: 자체 UA, robots `*` 규칙 준수, 요청 사이 간격, 목록·사실(제목·일시·장소·링크)만 보관, 예약·결제·대기열 경로는 건드리지 않는다.

출처: 예매처 오픈 공지(NOL·YES24·멜론·티켓링크·세종문화회관·LG아트센터 — open/presale/조회수) · 현대카드 DIVE · 나이키 SNKRS(drop) · 넷플릭스 뉴스룸(screen) · 팝업(팝플리·팝가) · 기업 원천(롯데월드몰·라인프렌즈·스타벅스·포켓몬)
      · 보도자료(뉴스와이어) · 큐레이션(헤이팝·디에디트).
흐름: 출처별 새 항목 감지(id diff·피드) → 후보 정규화 → data/trend/candidates.json (최근 120일 유지)
      상태(본 id·최대 id)는 data/trend/state.json. 채점·선별은 매일 Claude 루틴이 한다(층 2).
"""
import io, json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape
from xml.etree import ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "trend")
KST = timezone(timedelta(hours=9))
NOW = datetime.now(KST)
UA = "TrendsetterBot/0.1 (+https://signal.sharktalk.co.kr; feeds@sharktalk.co.kr)"
GAP = 0.6           # 요청 사이 간격(초)
KEEP_DAYS = 120     # 후보 보관 기간
BOOT = 40           # 처음 실행 때 출처별로 거꾸로 가져올 최대 개수
LOG = []


def log(*a):
    s = " ".join(str(x) for x in a)
    LOG.append(s)
    print(s, flush=True)


def fetch(url, data=None, ctype=None, tries=2):
    hdr = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
    if ctype:
        hdr["Content-Type"] = ctype
    body = urllib.parse.urlencode(data).encode() if isinstance(data, dict) else data
    for i in range(tries):
        try:
            time.sleep(GAP)
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=hdr), timeout=40) as r:
                raw = r.read()
                cs = r.headers.get_content_charset() or "utf-8"
                try:
                    return raw.decode(cs)
                except (UnicodeDecodeError, LookupError):
                    # 한국 사이트 일부는 charset 표시 없이 EUC-KR(CP949)
                    try:
                        return raw.decode("cp949")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
        except Exception as e:
            if i == tries - 1:
                log("  ! 실패", url[:90], type(e).__name__, str(e)[:80])
            time.sleep(3)
    return None


def load(p, d):
    try:
        return json.load(io.open(p, encoding="utf-8"))
    except Exception:
        return d


def save(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with io.open(p, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def d10(s):
    """여러 날짜 표기를 YYYY-MM-DD 로."""
    if not s:
        return None
    s = str(s).strip()
    m = re.match(r"(\d{4})[.\-/]?\s?(\d{1,2})[.\-/]?\s?(\d{1,2})", s)
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else None


def jsonld_events(html):
    out = []
    for blk in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            j = json.loads(blk.strip())
        except Exception:
            continue
        for x in (j if isinstance(j, list) else [j]):
            if isinstance(x, dict) and x.get("@type") in ("Event", "ExhibitionEvent", "MusicEvent", "TheaterEvent", "SaleEvent"):
                out.append(x)
    return out


def place(ev):
    loc = ev.get("location") or {}
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    if isinstance(loc, str):
        return loc
    addr = loc.get("address") or {}
    if isinstance(addr, dict):
        addr = " ".join(filter(None, [addr.get("addressRegion"), addr.get("addressLocality"), addr.get("streetAddress")]))
    return " · ".join(filter(None, [loc.get("name"), addr if isinstance(addr, str) else None]))


def rsc_field(html, name):
    """Next.js RSC 페이로드(이스케이프된 JSON)에서 첫 번째 "name":"값" 을 꺼낸다."""
    t = html.replace('\\"', '"')
    m = re.search(r'"%s":\s*("([^"]*)"|-?\d+(?:\.\d+)?|true|false|null)' % re.escape(name), t)
    if not m:
        return None
    return m.group(2) if m.group(2) is not None else m.group(1)


def sitemap_ids(url, pat):
    x = fetch(url)
    if not x:
        return []
    return sorted({int(i) for i in re.findall(pat, x)})


# ───────────────────────── 출처 ─────────────────────────

def src_popply(state):
    """팝플리 — sitemap id 증가분 → 상세의 JSON-LD Event + RSC(createdAt·사전예약·관심 수)."""
    ids = sitemap_ids("https://popply.co.kr/sitemap.xml", r"popply\.co\.kr/popup/(\d+)")
    if not ids:
        return []
    last = state.get("popply_max")
    new = [i for i in ids if last is None or i > last][-BOOT:] if last is not None else ids[-BOOT:]
    out = []
    for pid in new:
        url = f"https://popply.co.kr/popup/{pid}"
        h = fetch(url)
        if not h:
            continue
        evs = jsonld_events(h)
        ev = evs[0] if evs else {}
        title = ev.get("name") or (re.search(r"<title>(.*?)</title>", h, re.S) or [None, ""])[1]
        out.append({
            "source": "popply", "kind": "popup", "id": f"popply:{pid}", "url": url,
            "title": unescape(str(title)).strip()[:200],
            "start": d10(ev.get("startDate") or rsc_field(h, "startDate")), "end": d10(ev.get("endDate") or rsc_field(h, "endDate")),
            "venue": place(ev)[:160],
            "created_at": rsc_field(h, "createdAt"),
            "prereg": {k: rsc_field(h, k) for k in ("preRegister", "preRegisterStartDate", "preRegisterEndDate", "reservationExposureAt")},
            "region": rsc_field(h, "topLevelAddress"),
            "signals": popply_signals(h),
        })
    state["popply_max"] = max(ids)
    return out


def popply_signals(h):
    """관심 지표 — totalUserLike 가 실제 찜(좋아요). totalFavorites 는 늘 0 이라 쓰지 않는다(2026-10-11 조사, 471건 전부 0)."""
    out = {}
    for k, name in (("views", "views"), ("eventViewCount", "eventViewCount"), ("totalUserLike", "likes")):
        v = rsc_field(h, k)
        try:
            out[name] = int(float(v))
        except (TypeError, ValueError):
            out[name] = None
    return out


REFRESH_MAX = 60   # 하루 다시 재는 팝플리 상세 수(× GAP 0.6초)


def refresh_popply(items, state):
    """시작이 오늘-7일 ~ +21일인 팝플리 후보를 하루 한 번 다시 읽어 관심 지표 시계열(series)을 쌓는다.
    대란 팝업은 등록→행동 리드 9일 사이에 조회가 쌓인다 — 처음 본 값 하나로는 속도를 못 본다."""
    today = NOW.strftime("%Y-%m-%d")
    lo, hi = (NOW - timedelta(days=7)).strftime("%Y-%m-%d"), (NOW + timedelta(days=21)).strftime("%Y-%m-%d")
    due = [c for c in items.values() if c.get("source") == "popply" and c.get("start") and lo <= c["start"] <= hi
           and not any(s[0] == today for s in c.get("series", []))]
    due.sort(key=lambda c: c["start"])
    n = 0
    for c in due[:REFRESH_MAX]:
        h = fetch(c["url"])
        if not h:
            continue
        sig = popply_signals(h)
        if sig.get("views") is None:
            continue
        c["signals"] = sig
        c.setdefault("region", rsc_field(h, "topLevelAddress"))
        c.setdefault("series", []).append([today, sig["views"], sig.get("eventViewCount"), sig.get("likes")])
        c["series"] = c["series"][-40:]
        n += 1
    return n


def popply_hints(items):
    """S1 조회 속도의 '같은 주 등록분' 백분위 + S2 좋아요/조회 비율 → hint (채점자에게 숫자로 준다).
    원값 순위는 쓰지 않는다(광고성 팝업이 조회 1위였다). 비율 < 0.05% 면 조회 근거에서 뺀다(ad_suspect)."""
    def week(c):
        d = (c.get("created_at") or c.get("first_seen") or "")[:10]
        try:
            y, w, _ = datetime.strptime(d, "%Y-%m-%d").isocalendar()
            return f"{y}-W{w:02d}"
        except ValueError:
            return None

    def speed(c):
        ser = [s for s in c.get("series", []) if s[1] is not None]
        if len(ser) >= 2:
            d0, d1 = datetime.strptime(ser[0][0], "%Y-%m-%d"), datetime.strptime(ser[-1][0], "%Y-%m-%d")
            return (ser[-1][1] - ser[0][1]) / max(1, (d1 - d0).days)
        v = (c.get("signals") or {}).get("views")
        try:
            age = (NOW.replace(tzinfo=None) - datetime.strptime((c.get("created_at") or "")[:10], "%Y-%m-%d")).days
        except ValueError:
            return None
        return v / max(1, age) if isinstance(v, (int, float)) else None

    pops = [c for c in items.values() if c.get("source") == "popply"]
    groups = {}
    for c in pops:
        sp = speed(c)
        if sp is not None and week(c):
            groups.setdefault(week(c), []).append((sp, c))
    allv = sorted(sp for g in groups.values() for sp, _ in g)
    for wk, g in groups.items():
        base = sorted(sp for sp, _ in g) if len(g) >= 8 else allv   # 코호트가 작으면 전체로
        for sp, c in g:
            pct = round(100 * sum(1 for x in base if x <= sp) / len(base))
            sig = c.get("signals") or {}
            v, lk = sig.get("views"), sig.get("likes")
            ratio = round(lk / v, 5) if isinstance(v, int) and v > 0 and isinstance(lk, int) else None
            c["hint"] = {"s1_pct": pct, "s1_speed": round(sp, 1), "cohort": wk if len(g) >= 8 else "전체", "cohort_n": len(base),
                         "s2_ratio": ratio, "ad_suspect": bool(ratio is not None and v >= 1000 and ratio < 0.0005)}


def nol_ranking(state):
    """NOL 장르 랭킹(예매율·순위 변화) — 하루 4번까지. data/trend/nol_rank.json 에 지금 순위 + 10일 이력(S5)."""
    last = state.get("nol_rank_at")
    if last and NOW - datetime.fromisoformat(last) < timedelta(hours=6):
        return None
    path = os.path.join(OUT, "nol_rank.json")
    old = load(path, {"genres": {}, "history": {}})
    genres, hist = {}, old.get("history", {})
    stamp = NOW.strftime("%Y-%m-%d %H:%M")
    for g in ("concert", "musical", "play", "classic", "exhibit"):
        h = fetch(f"https://nol.yanolja.com/ticket/display/ranking/{g}")
        if not h:
            continue
        rows, seen = [], set()
        for o in rsc_objects(h, r'\{"id":"\d+_\d+_\d+","action"'):
            code = o.get("goodsCode")
            if not code or code in seen:
                continue
            seen.add(code)
            rk, ch = o.get("ranking") or {}, o.get("rankingChange") or {}
            rows.append({"rank": o.get("rank"), "code": code, "name": o.get("goodsName"), "rate": rk.get("bookingRate"),
                         "prev": rk.get("prevRankingNo"), "change": ch.get("status"), "new": ch.get("isNew"),
                         "place": o.get("placeName"), "period": o.get("playPeriod"), "seat": o.get("seatTypeName"),
                         "url": (o.get("action") or {}).get("web")})
            hist.setdefault(code, {"name": o.get("goodsName"), "genre": g, "s": []})["s"].append([stamp, o.get("rank"), rk.get("bookingRate")])
        if rows:
            genres[g] = rows
    cut = (NOW - timedelta(days=10)).strftime("%Y-%m-%d")
    for code in list(hist):
        hist[code]["s"] = [x for x in hist[code]["s"] if x[0] >= cut]
        if not hist[code]["s"]:
            del hist[code]
    save(path, {"updated": stamp, "genres": genres or old.get("genres", {}), "history": hist})
    state["nol_rank_at"] = NOW.isoformat()
    return sum(len(v) for v in genres.values())


YT_CATS = {"all": None, "music": "10", "entertainment": "24"}   # 전체·음악·엔터 — 각 1 단위(무료 할당 하루 10,000)


def youtube_popular(state):
    """YouTube Data API 한국 인기 차트 — '파급' 신호(S 조사 2026-10-11 TOP 3). 6시간 간격, data/trend/yt_popular.json.
    키는 환경변수 YOUTUBE_API_KEY(GitHub Secrets) — 주소가 아니라 헤더로 보내 로그·오류 문구에 남지 않게 한다.
    YouTube API 약관: 저장한 API 데이터는 30일 안에 갱신·삭제 → 이력은 7일만 둔다."""
    key = os.environ.get("YOUTUBE_API_KEY")
    if not key:
        return None
    last = state.get("yt_at")
    if last and NOW - datetime.fromisoformat(last) < timedelta(hours=6):
        return None
    path = os.path.join(OUT, "yt_popular.json")
    old = load(path, {"charts": {}, "history": {}})
    charts, hist = {}, old.get("history", {})
    stamp = NOW.strftime("%Y-%m-%d %H:%M")
    for name, cat in YT_CATS.items():
        q = {"part": "snippet,statistics", "chart": "mostPopular", "regionCode": "KR", "hl": "ko", "maxResults": "50"}
        if cat:
            q["videoCategoryId"] = cat
        url = "https://www.googleapis.com/youtube/v3/videos?" + urllib.parse.urlencode(q)
        try:
            time.sleep(GAP)
            req = urllib.request.Request(url, headers={"X-Goog-Api-Key": key, "User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            log("  ! YouTube", name, type(e).__name__, str(getattr(e, "code", "")))   # 키·주소는 남기지 않는다
            continue
        rows = []
        for i, v in enumerate(data.get("items", []), 1):
            sn, st = v.get("snippet") or {}, v.get("statistics") or {}
            rows.append({"rank": i, "id": v.get("id"), "title": sn.get("title"), "channel": sn.get("channelTitle"),
                         "published": sn.get("publishedAt"), "cat": sn.get("categoryId"),
                         "views": num(st.get("viewCount")), "likes": num(st.get("likeCount")),
                         "url": f"https://www.youtube.com/watch?v={v.get('id')}"})
            h = hist.setdefault(v.get("id"), {"title": sn.get("title"), "channel": sn.get("channelTitle"), "s": []})
            h["s"].append([stamp, name, i, num(st.get("viewCount"))])
        charts[name] = rows
    if not charts:
        return 0
    cut = (NOW - timedelta(days=7)).strftime("%Y-%m-%d")
    for vid in list(hist):
        hist[vid]["s"] = [x for x in hist[vid]["s"] if x[0] >= cut]
        if not hist[vid]["s"]:
            del hist[vid]
    save(path, {"updated": stamp, "charts": {**old.get("charts", {}), **charts}, "history": hist})
    state["yt_at"] = NOW.isoformat()
    return sum(len(v) for v in charts.values())


def rsc_objects(h, start_pat):
    """Next.js RSC 페이로드를 이어 붙여 start_pat 로 시작하는 JSON 객체들을 꺼낸다."""
    chunks = []
    for m in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)</script>", h or "", re.S):
        try:
            a = json.loads(m.group(1))
        except Exception:
            continue
        if len(a) > 1 and isinstance(a[1], str):
            chunks.append(a[1])
    s, dec = "".join(chunks), json.JSONDecoder()
    for m in re.finditer(start_pat, s):
        try:
            yield dec.raw_decode(s, m.start())[0]
        except Exception:
            continue


def src_popga(state):
    """팝가 — sitemap/2 id 집합 diff(id·lastmod 가 생성 순서와 다름) → 상세 JSON-LD + createdAt."""
    ids = sitemap_ids("https://popga.co.kr/sitemap/2.xml", r"popga\.co\.kr/popup/(\d+)")
    if not ids:
        return []
    seen = set(state.get("popga_seen", []))
    new = sorted(set(ids) - seen) if seen else ids[-BOOT:]
    new = new[-BOOT * 2:]
    out = []
    for pid in new:
        url = f"https://popga.co.kr/popup/{pid}"
        h = fetch(url)
        if not h:
            continue
        evs = jsonld_events(h)
        ev = evs[0] if evs else {}
        out.append({
            "source": "popga", "kind": "popup", "id": f"popga:{pid}", "url": url,
            "title": unescape(str(ev.get("name") or "")).strip()[:200],
            "start": d10(ev.get("startDate")), "end": d10(ev.get("endDate")), "venue": place(ev)[:160],
            "created_at": rsc_field(h, "createdAt"), "tags": ev.get("tags"),
        })
    state["popga_seen"] = ids  # 전체 집합(약 7천 개 정수)
    return out


def ko(v):
    """다국어 필드({ko,en,…})면 한국어를."""
    if isinstance(v, dict):
        return v.get("ko") or v.get("KO") or next((x for x in v.values() if x), "")
    return v or ""


def src_lotteworldmall(state):
    j = fetch("https://www.lwt.co.kr/api/event/list?lang=KO&pageNum=1&pageRow=100")
    try:
        rows = json.loads(j or "{}")
    except Exception:
        return []
    if isinstance(rows, dict):
        rows = rows.get("data") or rows.get("list") or next((v for v in rows.values() if isinstance(v, list)), [])
    out = []
    for r in rows if isinstance(rows, list) else []:
        if not isinstance(r, dict) or not r.get("seq"):
            continue
        out.append({"source": "lotteworldmall", "kind": "event", "id": f"lwt:{r['seq']}",
                    "url": f"https://www.lwt.co.kr/event/detail.do?seq={r['seq']}",
                    "title": unescape(str(ko(r.get("eventNm"))))[:200], "start": d10(r.get("startDate")), "end": d10(r.get("endDate")),
                    "venue": (lambda v: v if v.startswith("롯데월드몰") else ("롯데월드몰 " + v).strip())(str(ko(r.get("loc")))), "category": ko(r.get("mainCategory"))})
    return out


def src_atom(url, source, kind):
    x = fetch(url)
    if not x:
        return []
    ns = {"a": "http://www.w3.org/2005/Atom"}
    try:
        root = ET.fromstring(x)
    except Exception:
        return []
    out = []
    for e in root.findall("a:entry", ns)[:30]:
        link = e.find("a:link", ns)
        href = link.get("href") if link is not None else None
        out.append({"source": source, "kind": kind, "id": f"{source}:{(e.findtext('a:id', '', ns) or href)[-60:]}", "url": href,
                    "title": (e.findtext("a:title", "", ns) or "").strip()[:200], "created_at": e.findtext("a:published", None, ns)})
    return out


def src_rss(url, source, kind, limit=30):
    x = fetch(url)
    if not x:
        return []
    try:
        root = ET.fromstring(x.encode("utf-8") if isinstance(x, str) else x)
    except Exception:
        return []
    out = []
    for it in root.iter("item"):
        if len(out) >= limit:
            break
        link = (it.findtext("link") or "").strip()
        out.append({"source": source, "kind": kind, "id": f"{source}:{link[-80:]}", "url": link,
                    "title": unescape((it.findtext("title") or "").strip())[:200], "created_at": it.findtext("pubDate")})
    return out


def src_starbucks(state):
    j = fetch("https://www.starbucks.co.kr/whats_new/newsListAjax.do", data={"pageIndex": "1"},
              ctype="application/x-www-form-urlencoded; charset=UTF-8")
    try:
        rows = json.loads(j or "{}").get("list") or []
    except Exception:
        return []
    out = []
    for r in rows[:30]:
        out.append({"source": "starbucks", "kind": "md", "id": f"starbucks:{r.get('seq')}",
                    "url": f"https://www.starbucks.co.kr/whats_new/newsView.do?seq={r.get('seq')}",
                    "title": unescape(str(r.get("title") or ""))[:200], "created_at": r.get("reg_dt"),
                    "start": d10(r.get("start_dt") or r.get("news_dt")), "end": d10(r.get("end_dt")), "category": r.get("cate_nm")})
    return out


def src_pokemon(state):
    # ⚠ 고정 파라미터 외 값은 절대 보내지 않는다(응답에 서버 SQL 이 노출되는 엔드포인트 — 출처 등록부)
    h = fetch("https://pokemonkorea.co.kr/ajax/news", data={"pn": "1", "cate": "0", "sword": "", "rcode": "menu_news"},
              ctype="application/x-www-form-urlencoded; charset=UTF-8")
    if not h:
        return []
    out, seen = [], set()
    # 목록 한 칸 = <li> 안의 <a href=…> + <h3>제목</h3>
    for li in re.findall(r"<li class=\"col-[^\"]*\">(.*?)</li>\s*(?=<li class=\"col-|$)", h, re.S):
        a = re.search(r'<a href="([^"]+)"', li)
        t = re.search(r"<h3>(.*?)</h3>", li, re.S)
        if not a or not t:
            continue
        href = a.group(1) if a.group(1).startswith("http") else "https://pokemonkorea.co.kr" + a.group(1)
        key = (re.search(r"number=(\d+)", href) or re.search(r"/news/\d+/(\d+)", href) or [None, href[-40:]])[1]
        if key in seen:
            continue
        seen.add(key)
        title = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", t.group(1)))).strip()
        if title:
            out.append({"source": "pokemon", "kind": "event", "id": f"pokemon:{key}", "url": href, "title": title[:200]})
    return out[:31]


def src_newswire(state):
    x = fetch("https://www.newswire.co.kr/sitemap/news")
    if not x:
        return []
    out = []
    for blk in re.findall(r"<url>(.*?)</url>", x, re.S):
        loc = re.search(r"<loc>(.*?)</loc>", blk)
        title = re.search(r"<news:title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</news:title>", blk, re.S)
        pub = re.search(r"<news:publication_date>(.*?)</news:publication_date>", blk)
        if not loc or not title:
            continue
        t = unescape(title.group(1)).strip()
        # 보도자료는 하루 수십 건 — 문화·이벤트성 제목만 남긴다(채점은 루틴이)
        if not re.search(r"팝업|오픈|출시|한정|콜라보|컬래버|전시|공연|페스티벌|축제|굿즈|사전예약|예약|선착순|응모|론칭|런칭|기념|에디션|체험|행사|개최|내한|시즌", t):
            continue
        u = unescape(loc.group(1))
        out.append({"source": "newswire", "kind": "press", "id": "newswire:" + (re.search(r"no=(\d+)", u) or [None, u[-40:]])[1],
                    "url": u, "title": t[:200], "created_at": pub.group(1) if pub else None})
    return out


# ───────── 예매처 오픈 공지 — '언제 열리나'(open = 일반 예매 KST, presale = 선예매) ─────────
# 목록 페이지만 읽는다. 예매·대기열·좌석 경로(/reserve, NetFunnel 등)는 절대 호출하지 않는다.

def dt16(s):
    """'2026.10.14(수) 15:00' · '2026-10-13T11:00:00' → '2026-10-14 15:00'."""
    m = re.search(r"(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})\D{0,6}?(\d{1,2}):(\d{2})", s or "")
    return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d} {int(m.group(4)):02d}:{m.group(5)}" if m else None


def num(s):
    try:
        return int(re.sub(r"[^\d]", "", str(s)))
    except ValueError:
        return None


def src_yes24(state):
    out = []
    for page in (1, 2):
        h = fetch("https://ticket.yes24.com/New/Notice/Ajax/axList.aspx",
                  data={"page": str(page), "size": "20", "genre": "", "province": "", "order": "1", "searchType": "All", "searchText": ""},
                  ctype="application/x-www-form-urlencoded; charset=UTF-8")
        if not h:
            break
        for tr in re.findall(r"<tr>(.*?)</tr>", h, re.S):
            tds = re.findall(r"<td>(.*?)</td>", tr, re.S)
            a = re.search(r'href="#id=(\d+)"', tr)
            if len(tds) < 4 or not a or "티켓오픈" not in tds[0]:
                continue
            # 제목 안의 〈부제〉가 이스케이프 없이 '<…>' 로 오므로 아는 태그만 벗긴다
            tag = lambda x: re.sub(r"</?(?:span|em|a|b|strong|br|font)\b[^>]*>", "", x, flags=re.I)
            ems = [tag(e).strip() for e in re.findall(r"<em>(.*?)</em>", tds[1], re.S)]
            title = re.sub(r"\s+", " ", unescape(ems[-1] if ems else tag(tds[1]))).strip()
            pre = [{"name": unescape(n), "at": dt16(t)} for n, t in
                   re.findall(r"presaleTit\d='([^']+)'[^>]*?presaleTime\d='([^']+)'", tds[2]) if dt16(t)]
            out.append({"source": "yes24", "kind": "ticket", "id": f"yes24:{a.group(1)}",
                        "url": f"https://ticket.yes24.com/New/Notice/NoticeMain.aspx#id={a.group(1)}",
                        "title": title[:200], "open": dt16(tds[2]), "presale": pre or None,
                        "exclusive": any("단독" in e for e in ems[:-1]) or None,
                        "signals": {"views": num(re.sub(r"<[^>]+>", "", tds[3]))}})
    return out


def src_melon(state):
    out = []
    for page in (1, 2):  # 페이지가 많이 겹친다 — 3시간마다 돌므로 두 쪽이면 충분
        h = fetch(f"https://ticket.melon.com/csoon/ajax/listTicketOpen.htm?orderType=0&pageIndex={page}&schGcode=GENRE_ALL")
        if not h:
            break
        for li in re.findall(r"<li>(.*?)</li>", h, re.S):
            a = re.search(r'csoonId=(\d+)"\s+class="tit">(.*?)</a>', li, re.S)
            if not a:
                continue
            d = re.search(r'class="date">(.*?)</span>', li, re.S)
            v = re.search(r'class="txt_review">(.*?)</dd>', li, re.S)
            reg = re.search(r'class="txt_date">(.*?)</dd>', li, re.S)
            out.append({"source": "melon", "kind": "ticket", "id": f"melon:{a.group(1)}",
                        "url": f"https://ticket.melon.com/csoon/detail.htm?csoonId={a.group(1)}",
                        "title": re.sub(r"\s+", " ", unescape(a.group(2))).strip()[:200],
                        "open": dt16(d.group(1) if d else ""), "exclusive": ("단독판매" in li) or None,
                        "created_at": d10(reg.group(1)) if reg else None,
                        "signals": {"views": num(v.group(1)) if v else None}})
    return out


def src_nol(state):
    """NOL 티켓(구 인터파크) 오픈예정 — 페이지에 실린 Next.js RSC 페이로드의 공지 객체를 그대로 읽는다."""
    h = fetch("https://nol.yanolja.com/ticket/display/upcoming")
    if not h:
        return []
    chunks = []
    for m in re.finditer(r"self\.__next_f\.push\((\[.*?\])\)</script>", h, re.S):
        try:
            a = json.loads(m.group(1))
        except Exception:
            continue
        if len(a) > 1 and isinstance(a[1], str):
            chunks.append(a[1])
    s, dec, seen, out = "".join(chunks), json.JSONDecoder(), set(), []
    for m in re.finditer(r'\{"ticket_dates":', s):
        try:
            o, _ = dec.raw_decode(s, m.start())
        except Exception:
            continue
        if o.get("id") in seen:
            continue
        seen.add(o.get("id"))
        dates = o.get("ticket_dates") or []
        gen = [d for d in dates if d.get("ticket_open_type") == 1] or dates
        code = o.get("goods_code")
        out.append({"source": "nol", "kind": "ticket", "id": f"nol:{o.get('id')}",
                    "url": f"https://nol.yanolja.com/ticket/products/{code}" if code else "https://nol.yanolja.com/ticket/display/upcoming",
                    "title": str(o.get("title") or "")[:200], "open": dt16(gen[0].get("ticket_open_date")) if gen else None,
                    "presale": [{"name": d.get("ticket_other_open_name") or d.get("ticket_open_type_name"), "at": dt16(d.get("ticket_open_date"))}
                                for d in dates if d not in gen[:1]] or None,
                    "open_type": o.get("open_type_name"), "genre": o.get("goods_genre_name"),
                    "venue": str(o.get("venue_name") or "").strip()[:160],
                    "start": o.get("goods_start_date"), "end": o.get("goods_end_date"),
                    "created_at": o.get("created_at"), "signals": {"views": o.get("view_count")}})
    return out


# ───────── 2026-10-13 추가 출처(실측 보고: 위키 log 2026-10-13) — 개인정보 필드(직원 이름·ID·홍보 담당 연락처)는 저장하지 않는다 ─────────

def ko_dt(s, ref=None):
    """'2026년 10월 16일(금) 오후 7시' · '9/30(수) 오후 2시' · '10월 15일 14:00' → 'YYYY-MM-DD HH:MM' (연도 없으면 가까운 미래로)."""
    s = s or ""
    m = re.search(r"(?:(\d{4})\s*[년.\-/]\s*)?(\d{1,2})\s*(?:월|[./])\s*(\d{1,2})\s*일?[^0-9]{0,12}?(오전|오후|낮|밤|저녁)?\s*(\d{1,2})\s*(?:시|:)\s*(\d{1,2})?", s)
    if not m:
        return None
    y, mo, d, ap, h, mi = m.groups()
    h = int(h) + (12 if ap in ("오후", "저녁", "밤") and int(h) < 12 else 0)
    ref = ref or NOW
    y = int(y) if y else (ref.year + (1 if int(mo) < ref.month - 6 else 0))
    try:
        return datetime(y, int(mo), int(d), h % 24, int(mi or 0)).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return None


def src_ticketlink(state):
    """티켓링크 티켓오픈 공지 — 공개 JSON(/help 는 robots 허용). /reserve·대기열은 부르지 않는다."""
    out = []
    for page in (1, 2):
        j = fetch(f"https://www.ticketlink.co.kr/help/getNoticeList?page={page}&noticeCategoryCode=TICKET_OPEN&sortCode=REGISTE_DATE")
        try:
            rows = ((json.loads(j or "{}").get("result") or {}).get("result")) or []
        except Exception:
            break
        for r in rows:
            nid = r.get("noticeId")
            if not nid:
                continue
            reg = r.get("registerDatetime")
            out.append({"source": "ticketlink", "kind": "ticket", "id": f"ticketlink:{nid}",
                        "url": f"https://www.ticketlink.co.kr/help/notice/{nid}",
                        "title": re.sub(r"\s+", " ", unescape(re.sub(r"</?b>", "", r.get("title") or ""))).strip()[:200],
                        "open": dt16(r.get("ticketOpenDatetime")), "venue": (r.get("placeName") or "")[:160],
                        "exclusive": ("단독" in (r.get("title") or "")) or None,
                        "created_at": datetime.fromtimestamp(reg / 1000, KST).strftime("%Y-%m-%d %H:%M") if isinstance(reg, (int, float)) else None,
                        "product": f"https://www.ticketlink.co.kr/product/{r['productId']}" if r.get("productId") else None,
                        "signals": {"views": r.get("viewCount")}})
    return out


def src_sejong(state):
    """세종문화회관 티켓오픈안내 — 정적 표(등록일·오픈일시·조회)."""
    h = fetch("https://www.sejongpac.or.kr/portal/bbs/B0000049/list.do?menuNo=200440")
    out = []
    for tr in re.findall(r"<tr>(.*?)</tr>", h or "", re.S):
        a = re.search(r'nttId=(\d+)[^"]*">(.*?)</a>', tr, re.S)
        tds = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if not a or len(tds) < 6:
            continue
        out.append({"source": "sejong", "kind": "ticket", "id": f"sejong:{a.group(1)}",
                    "url": f"https://www.sejongpac.or.kr/portal/bbs/B0000049/view.do?nttId={a.group(1)}&menuNo=200440",
                    "title": re.sub(r"\s+", " ", unescape(a.group(2))).strip()[:200], "venue": "세종문화회관",
                    "created_at": d10(tds[2]), "open": dt16(tds[3]), "signals": {"views": num(tds[5])}})
    return out


def src_dive(state):
    """현대카드 DIVE 컬처 캘린더(슈퍼콘서트·Curated·언더스테이지) — 목록 JSON + 새 항목만 상세의 '티켓 오픈'·'예매처'."""
    j = fetch("https://dive.hyundaicard.com/web/culture/getCultureOnePaging.hdc",
              data={"isNotPast": "Y", "pageCount": "30", "pageNo": "1"}, ctype="application/x-www-form-urlencoded; charset=UTF-8")
    try:
        d = json.loads(j or "{}")
    except Exception:
        return []

    def find(o):
        if isinstance(o, list) and o and isinstance(o[0], dict) and "contentId" in o[0]:
            return o
        if isinstance(o, dict):
            for v in o.values():
                r = find(v)
                if r:
                    return r
        return None

    seen = state.setdefault("dive_detail", {})
    out = []
    for r in find(d) or []:
        cid = str(r.get("contentId"))
        lib = r.get("libraryName") or ""
        if lib in ("COOKING LIBRARY", "고메위크", "호텔위크"):   # 쿠킹·식당·호텔 할인 주간은 문화 이벤트가 아니다(N5)
            continue
        if cid not in seen:
            h = fetch(f"https://dive.hyundaicard.com/web/content/contentView.hdc?contentId={cid}") or ""
            info = dict((t, v) for v, t in re.findall(r'"text":"([^"]{0,120})","title":"([^"]{0,20})"', urllib.parse.unquote(h)))
            seen[cid] = {"open": ko_dt(info.get("티켓 오픈") or info.get("티켓오픈") or ""), "where": info.get("예매처") or info.get("예매"),
                         "when": info.get("일시"), "venue": info.get("장소")}
        x = seen[cid]
        st, en = r.get("contentEventStartDate") or "", r.get("contentEventEndDate") or ""
        out.append({"source": "dive", "kind": "ticket" if x.get("open") else "event", "id": f"dive:{cid}",
                    "url": f"https://dive.hyundaicard.com/web/content/contentView.hdc?contentId={cid}",
                    "title": re.sub(r"<br\s*/?>", " ", unescape(r.get("contentTitle") or "")).strip()[:200],
                    "category": lib, "open": x.get("open"), "booking": x.get("where"), "venue": (x.get("venue") or "")[:160],
                    "start": d10(st), "end": d10(en), "created_at": (r.get("insertDate") or "")[:16] or None})
    return out


def src_netflix(state):
    """넷플릭스 코리아 뉴스룸(about.netflix.com — www 와 다른 호스트, robots 허용). 한국 기사만, 제목의 'n월 n일 공개'를 행사일로."""
    h = fetch("https://about.netflix.com/ko/newsroom")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', h or "", re.S)
    if not m:
        return []
    try:
        d = json.loads(m.group(1))
    except Exception:
        return []

    def walk(o):
        if isinstance(o, dict):
            if "slug" in o and "title" in o and "locations" in o:
                yield o
            for v in o.values():
                yield from walk(v)
        elif isinstance(o, list):
            for v in o:
                yield from walk(v)

    out, seen = [], set()
    for a in walk(d):
        if a["slug"] in seen or not any((l or {}).get("value") == "south-korea" for l in a.get("locations") or []):
            continue
        seen.add(a["slug"])
        t = re.sub(r"\s+", " ", a.get("title") or "").strip()
        rel = re.search(r"(\d{1,2})월\s*(\d{1,2})일[^,.!]{0,8}?(?:공개|첫 공개)", t)
        pub = a.get("rawPublishedDate") or a.get("publishedDate")
        out.append({"source": "netflix", "kind": "screen", "id": f"netflix:{a['slug'][:80]}",
                    "url": f"https://about.netflix.com/ko/news/{a['slug']}", "title": t[:200],
                    "start": ko_dt(f"{rel.group(1)}월 {rel.group(2)}일 0시")[:10] if rel and ko_dt(f"{rel.group(1)}월 {rel.group(2)}일 0시") else None,
                    "created_at": str(pub)[:16] if pub else None})
    return out[:30]


def src_lgart(state):
    """LG아트센터 공지 — 목록의 JSON(티켓 분류만) + 새 글만 상세에서 '티켓오픈' 일시."""
    h = fetch("https://www.lgart.com/community/ko/notice")
    seen = state.setdefault("lgart_detail", {})
    out = []
    for blk in re.findall(r'\{"ArticleID":\d+.*?"DetailsUrl":"[^"]*"\}', h or ""):
        try:
            a = json.loads(blk)
        except Exception:
            continue
        if a.get("CategoryName") != "티켓" and "티켓" not in (a.get("Title") or ""):
            continue
        aid = str(a["ArticleID"])
        if aid not in seen:
            dh = fetch(f"https://www.lgart.com/community/ko/notice/{aid}") or ""
            cm = re.search(r'"Contents":("(?:[^"\\]|\\.)*")', dh)          # 본문은 JSON 문자열(<…) 안에 있다
            try:
                body = json.loads(cm.group(1)) if cm else ""
            except Exception:
                body = ""
            txt = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", body)))
            mm = re.search(r"티켓\s*오픈[^0-9※]{0,14}?((?:20\d\d\s*[.\-/년]\s*)?\d{1,2}\s*[월./]\s*\d{1,2}[^※]{0,25})", txt)
            ref = datetime.strptime(a.get("CreateDate") or NOW.strftime("%Y-%m-%d"), "%Y-%m-%d")
            got = ko_dt(mm.group(1), ref) if mm else None
            if got:                                   # 못 찾은 글은 다음 실행 때 다시 읽는다
                seen[aid] = got
        out.append({"source": "lgart", "kind": "ticket", "id": f"lgart:{aid}", "url": f"https://www.lgart.com/community/ko/notice/{aid}",
                    "title": unescape(a.get("Title") or "")[:200], "venue": "LG아트센터 서울", "open": seen.get(aid),
                    "created_at": a.get("CreateTime"), "signals": {"views": a.get("ViewCount")}})
    return out


def src_snkrs(state):
    """나이키 SNKRS 출시 캘린더 — SSR 안의 launchViews(LINE 선착순 / DRAW 응모, 시작·마감 UTC)."""
    h = fetch("https://www.nike.com/kr/launch/upcoming?s=upcoming")
    if not h:
        return []
    u = h.replace('\\"', '"')
    i = u.find('"launchViews":{"status"')
    j = u.find('"data":{"items":', i) if i >= 0 else -1
    try:
        views = json.JSONDecoder().raw_decode(u, j + len('"data":{"items":'))[0] if j >= 0 else {}
    except Exception:
        return []
    out = []
    for pid, v in views.items():
        t = re.search(r'"%s":\{"title":"([^"]*)","subtitle":"([^"]*)"' % re.escape(pid), u)
        p = re.search(r'"%s":\{"commerceStartDate":[^{}]*?"currentPrice":(\d+)' % re.escape(pid), u)
        to_kst = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(KST).strftime("%Y-%m-%d %H:%M") if s else None
        title = " ".join(x for x in (t.group(1), t.group(2)) if x) if t else pid
        out.append({"source": "snkrs", "kind": "drop", "id": f"snkrs:{pid}", "url": "https://www.nike.com/kr/launch/upcoming",
                    "title": unescape(title)[:200], "method": v.get("method"),   # LINE=선착순, DRAW=응모
                    "open": to_kst(v.get("startEntryDate")), "close": to_kst(v.get("stopEntryDate")),
                    "price": int(p.group(1)) if p else None})
    return out


SOURCES = [
    ("nol", src_nol),
    ("ticketlink", src_ticketlink),
    ("sejong", src_sejong),
    ("lgart", src_lgart),
    ("dive", src_dive),
    ("snkrs", src_snkrs),
    ("netflix", src_netflix),
    ("yes24", src_yes24),
    ("melon", src_melon),
    ("popply", src_popply),
    ("popga", src_popga),
    ("lotteworldmall", src_lotteworldmall),
    ("linefriends_event", lambda s: src_atom("https://linefriendssquare.com/blogs/event.atom", "linefriends", "popup")),
    ("linefriends_drop", lambda s: src_atom("https://linefriendssquare.com/blogs/drop.atom", "linefriends", "drop")),
    ("starbucks", src_starbucks),
    ("pokemon", src_pokemon),
    ("newswire", src_newswire),
    ("heypop", lambda s: src_rss("https://heypop.kr/feed/?post_type=n", "heypop", "article")),
    ("theedit", lambda s: src_rss("https://the-edit.co.kr/feed", "theedit", "article")),
]


def main():
    only = set(sys.argv[1:])
    state = load(os.path.join(OUT, "state.json"), {})
    cands = load(os.path.join(OUT, "candidates.json"), {"items": []})
    items = {c["id"]: c for c in cands.get("items", [])}
    stamp = NOW.strftime("%Y-%m-%d %H:%M")
    stats = {}
    for name, fn in SOURCES:
        if only and name not in only:
            continue
        log("▶", name)
        try:
            got = fn(state) or []
        except Exception as e:
            log("  ! 오류", type(e).__name__, str(e)[:120])
            got = []
        new = 0
        for c in got:
            if not c.get("title"):
                continue
            old = items.get(c["id"])
            if old:
                old.update({k: v for k, v in c.items() if v not in (None, "", {}, [])})
                old["last_seen"] = stamp
            else:
                c["first_seen"] = stamp
                c["last_seen"] = stamp
                if c.get("signals"):
                    c["signals_first"] = dict(c["signals"])  # 증가 속도를 보려고 처음 값을 남긴다
                items[c["id"]] = c
                new += 1
        stats[name] = {"got": len(got), "new": new}
        log(f"  {len(got)}건 · 새 {new}")
    # 관심 지표 — 팝플리 재측정(하루 1회)·S1/S2 힌트, NOL 랭킹(하루 4회) → 예매 오픈 후보에 순위 붙이기
    if not only or "popply" in only:
        try:
            log("▶ 팝플리 재측정", refresh_popply(items, state), "건")
            popply_hints(items)
        except Exception as e:
            log("  ! 팝플리 재측정 오류", type(e).__name__, str(e)[:120])
    if not only or "nol" in only:
        try:
            n = nol_ranking(state)
            log("▶ NOL 랭킹", "건너뜀(6시간 안)" if n is None else f"{n}건")
        except Exception as e:
            log("  ! NOL 랭킹 오류", type(e).__name__, str(e)[:120])
    if not only or "youtube" in only:
        try:
            n = youtube_popular(state)
            log("▶ YouTube 인기", "건너뜀(키 없음·6시간 안)" if n is None else f"{n}건")
        except Exception as e:
            log("  ! YouTube 오류", type(e).__name__)
    rank = {}
    for g, rows in load(os.path.join(OUT, "nol_rank.json"), {}).get("genres", {}).items():
        for r in rows:
            rank[r["code"]] = {"genre": g, "rank": r["rank"], "rate": r["rate"], "change": r["change"], "new": r["new"]}
    for c in items.values():
        m = re.search(r"/ticket/products/(\d+)", c.get("url") or "")
        if c.get("source") == "nol" and m:
            if m.group(1) in rank:
                c["nol_rank"] = rank[m.group(1)]
            else:
                c.pop("nol_rank", None)
    # 오래된 것 정리: 끝난 지 KEEP_DAYS 지났거나, 날짜 없는 것은 처음 본 지 KEEP_DAYS 지난 것
    cutoff = (NOW - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    keep = [c for c in items.values() if (c.get("end") or c.get("first_seen", "")[:10]) >= cutoff]
    keep.sort(key=lambda c: c.get("first_seen", ""), reverse=True)
    save(os.path.join(OUT, "candidates.json"), {"updated": stamp, "count": len(keep), "items": keep})
    save(os.path.join(OUT, "state.json"), state)
    save(os.path.join(OUT, "last_run.json"), {"at": stamp, "stats": stats, "log": LOG[-80:]})
    log("완료", stamp, "후보", len(keep))


if __name__ == "__main__":
    main()
