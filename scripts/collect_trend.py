"""트렌드세터 수집기 — 층 1 (자동 수집, LLM 없음). GitHub Actions 3시간마다.

설계: 트렌드세터_LLM위키/wiki/운영/수집-파이프라인.md · 출처: 위키 출처-등록부 '기업 이벤트 출처' 절(2026-10-10 실측).
원칙: 자체 UA, robots `*` 규칙 준수, 요청 사이 간격, 목록·사실(제목·일시·장소·링크)만 보관, 예약·결제·대기열 경로는 건드리지 않는다.

출처: 예매처 오픈 공지(NOL·YES24·멜론 — open/presale/조회수) · 팝업(팝플리·팝가) · 기업 원천(롯데월드몰·라인프렌즈·스타벅스·포켓몬)
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
            "signals": {k: rsc_field(h, k) for k in ("totalFavorites", "eventViewCount", "views")},
        })
    state["popply_max"] = max(ids)
    return out


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


SOURCES = [
    ("nol", src_nol),
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
