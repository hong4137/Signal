"""해외 현지 중계 채널 — Sports UptoDate 축구 픽에 '어느 나라 어느 채널'을 붙인다.

사용자(2026-10-11): 쿠팡플레이 말고 현지 채널로 본다. 영국 → 미국(OTT 제외) → 캐나다(TSN 등)
                    → 라리가·분데스리가 경기면 스페인·독일 → beIN(아랍). 앞의 2개를 보여 준다.
출처: LiveSoccerTV 경기 페이지의 나라별 중계 표(robots 허용 경로만 — /xloadday.php 등 금지 경로는 부르지 않음).
흐름: 팀 페이지(홈 → 원정) 또는 대회 페이지에서 킥오프 시각(±3시간)으로 경기 링크를 찾고 → 경기 페이지 표를 읽는다.
      결과는 data/world_tv_cache.json 에 12시간 보관(3시간마다 도는 수집기가 매번 부르지 않게).
실패하면 아무것도 바꾸지 않는다(리그 기본 문구 유지).
"""
import io, json, os, re, time, unicodedata, urllib.request
from datetime import datetime, timedelta, timezone
from html import unescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "world_tv_cache.json")
BASE = "https://www.livesoccertv.com"
UA = {"User-Agent": "SportsUptoDateBot/0.1 (+https://signal.sharktalk.co.kr)"}
TTL = timedelta(hours=12)

# 대회 → LiveSoccerTV 경로와 팀 페이지 나라
COMP = {
    "soccer/eng.1": ("/competitions/england/premier-league/", "england"),
    "soccer/eng.2": ("/competitions/england/championship/", "england"),
    "soccer/esp.1": ("/competitions/spain/primera-division/", "spain"),
    "soccer/ger.1": ("/competitions/germany/bundesliga/", "germany"),
    "soccer/ita.1": ("/competitions/italy/serie-a/", "italy"),
    "soccer/fra.1": ("/competitions/france/ligue-1/", "france"),
    "soccer/por.1": ("/competitions/portugal/liga-sagres/", "portugal"),
    "soccer/usa.1": ("/competitions/united-states/major-league-soccer/", "united-states"),
    "soccer/uefa.champions": ("/competitions/international/uefa-champions-league/", None),
    "soccer/uefa.europa": ("/competitions/international/uefa-europa-league/", None),
}
# ESPN 팀 이름 → LiveSoccerTV 팀 주소 조각(나라/슬러그). 이름을 그대로 슬러그로 바꿔 안 맞는 것만.
TEAM_SLUG = {
    "Bayern Munich": "germany/bayern-munchen", "Borussia Mönchengladbach": "germany/borussia-monchengladbach",
    "Atlético Madrid": "spain/atletico-madrid", "Athletic Club": "spain/athletic-bilbao", "Alavés": "spain/deportivo-alaves",
    "Tottenham Hotspur": "england/tottenham-hotspur", "AFC Bournemouth": "england/afc-bournemouth",
    "Internazionale": "italy/inter-milan", "AC Milan": "italy/ac-milan",
    "Paris Saint-Germain": "france/psg", "Benfica": "portugal/benfica-lisbon", "FC Porto": "portugal/fc-porto",
    "LAFC": "united-states/los-angeles-fc", "Inter Miami CF": "united-states/inter-miami",
    # 유럽대항전(나라를 모르는 대회)에서 스페인·독일 팀을 알아보려고 — 슬러그 2026-10-11 확인
    "Real Madrid": "spain/real-madrid", "Barcelona": "spain/barcelona", "Villarreal": "spain/villarreal",
    "Sevilla": "spain/sevilla", "Real Betis": "spain/real-betis",
    "Bayer Leverkusen": "germany/bayer-leverkusen", "Borussia Dortmund": "germany/borussia-dortmund",
    "RB Leipzig": "germany/rb-leipzig", "Eintracht Frankfurt": "germany/eintracht-frankfurt", "VfB Stuttgart": "germany/vfb-stuttgart",
}

# 시즌 단위 중계권 — 경기마다 안 바뀌어 고정표로(매 시즌 확인). 나라 순서·OTT 제외 규칙은 축구와 같다.
# F1 2026(확인 2026-10-11): 영국 Sky Sports F1 · 미국 Apple TV(OTT → 제외) · 캐나다 TSN/RDS(Bell 독점 보도자료 2026-03-04)
#                          · MENA beIN SPORTS(2024~2033 10년 계약)
SEASON = {
    "racing/f1": [("Great Britain", "영국", "Sky Sports F1"), ("Canada", "캐나다", "TSN"), ("MENA", "아랍", "beIN SPORTS")],
}

# 보여 줄 나라 순서(라리가·분데스리가는 스페인·독일을 beIN 앞에)
ORDER = [("Great Britain", "영국"), ("USA", "미국"), ("Canada", "캐나다")]   # 국기 이모지는 윈도 크롬에서 글자로 깨진다
LOCAL = {"soccer/esp.1": [("Spain", "스페인")], "soccer/ger.1": [("Germany", "독일")]}
LOCAL_BY_TEAM = {"spain": ("Spain", "스페인"), "germany": ("Germany", "독일")}   # 유럽대항전에 스페인·독일 팀이 나오면
ARAB = ["Qatar", "Saudi Arabia", "United Arab Emirates"]          # beIN 은 아랍권 표에서 찾는다
# OTT·앱·라디오·업소용은 뺀다(사용자: OTT 제외). 이름에 이 조각이 있으면 제외.
SKIP = re.compile(r"fubo|peacock|paramount|espn\+|tsn\+|sportsnet\+|dazn canada|hbo max|prime video|amazon|apple tv|"
                  r"sky go|\bnow\b|now tv|now player|wow\b|\btod\b|connect|siriusxm|radio|talksport|"
                  r"en vivo|universo now|vix|stream|\bapp\b|youtube|bar\b|\bgo\b|golazo|onesoccer|ea sports|\.com\b", re.I)
# 같은 나라 안 우선순위(앞일수록 먼저). 목록에 없으면 표에 나온 순서.
PREFER = {
    "Great Britain": ["Sky Sports Main Event", "Sky Sports Premier League", "Sky Sports Football", "TNT Sports 1",
                      "TNT Sports 2", "TNT Sports 3", "TNT Sports 4", "BBC One", "BBC Two", "ITV1", "Channel 4",
                      "Premier Sports 1", "Premier Sports 2", "Sky Sports+", "Sky Sports Mix"],
    "USA": ["NBC", "USA Network", "CBS", "FOX", "FS1", "FS2", "ESPN", "ESPN2", "TNT", "truTV", "CBS Sports Network",
            "NBCSN", "Telemundo", "Universo", "ESPN Deportes", "Fox Deportes", "TUDN", "Univision", "UniMás"],
    "Canada": ["TSN1", "TSN2", "TSN3", "TSN4", "TSN5", "TSN", "Sportsnet", "Sportsnet One", "CTV", "TVA Sports", "RDS"],
    "Spain": ["M+ LaLiga", "M+ LaLiga TV", "Movistar LaLiga", "Movistar Liga de Campeones", "Movistar+", "Movistar Plus+", "DAZN LaLiga", "DAZN1 Spain", "DAZN Spain"],
    "Germany": ["Sky Sport Bundesliga", "Sky Sport Top Event", "Sky Sport Premier League", "Sat.1", "ZDF", "ARD", "DAZN 1"],
}
LOW = re.compile(r"ultra hd|ultimate|4k|uhd", re.I)   # 4K 판은 같은 나라 다른 채널이 없을 때만


def _get(url):
    time.sleep(1.0)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception as e:
        print("  ! 현지 중계 실패", url[:90], type(e).__name__)
        return None


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def _slug(name):
    return _norm(name).replace(" ", "-")


def _rows(html):
    """목록 페이지의 경기 줄 → [(킥오프 epoch 초, 제목, 경기 주소)]"""
    out = []
    for m in re.finditer(r"<tr id=\"\d+\" class=\"matchrow\".*?dv='(\d+)'.*?<a href=\"(/match/[^\"#]+)[^\"]*\" title=\"([^\"]+)\"", html or "", re.S):
        out.append((int(m.group(1)) // 1000, unescape(m.group(3)), m.group(2)))
    return out


def _same_team(a, b):
    wa, wb = set(_norm(a).split()) - {"fc", "cf", "afc", "sc", "ac"}, set(_norm(b).split()) - {"fc", "cf", "afc", "sc", "ac"}
    return bool(wa & wb)


def _find(ev, pages):
    t0 = int(datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).timestamp())
    hn, an = ev["home"]["name"], ev["away"]["name"]
    for html, team_page in pages:
        near = [(title, href) for ts, title, href in _rows(html) if abs(ts - t0) <= 3 * 3600]
        if team_page and near:
            return near[0][1]
        both = [h for t, h in near if _same_team(hn, t) and _same_team(an, t)]
        one = [h for t, h in near if _same_team(hn, t) or _same_team(an, t)]   # 'LAFC' ↔ 'Los Angeles FC' 같은 경우
        if both or len(one) == 1:
            return (both or one)[0]
    return None


def _channels(html):
    """경기 페이지 표 → {나라: [채널…]}"""
    out = {}
    for c, cell in re.findall(r"<span class=\"flag [^\"]+\">([^<]+)</span></td><td valign='top'>(.*?)</td></tr>", html or "", re.S):
        out[unescape(c).strip()] = [unescape(x).strip() for x in re.findall(r'class="black nou">(.*?)</a>', cell)]
    return out


def _best(country, chans):
    ok = [c for c in chans if not SKIP.search(c)]
    if not ok:
        return None
    pref = PREFER.get(country, [])
    ok.sort(key=lambda c: (bool(LOW.search(c)), pref.index(c) if c in pref else len(pref) + chans.index(c)))
    return ok[0]


def pick_channels(path, table, teams=()):
    """나라 순서대로 고른 [{country, label, ch}] (전부)."""
    extra = [LOCAL_BY_TEAM[TEAM_SLUG[t].split("/")[0]] for t in teams
             if t in TEAM_SLUG and TEAM_SLUG[t].split("/")[0] in LOCAL_BY_TEAM and path.startswith("soccer/uefa")]
    order = ORDER + LOCAL.get(path, []) + [x for i, x in enumerate(extra) if x not in extra[:i]]
    out = []
    for country, label in order:
        ch = _best(country, table.get(country, []))
        if ch:
            out.append({"country": country, "label": label, "ch": ch})
    for country in ARAB:
        bein = [c for c in table.get(country, []) if "bein" in c.lower()]
        ch = _best(country, bein)
        if ch:
            out.append({"country": country, "label": "아랍", "ch": ch})
            break
    return out


def attach(picks, show=2):
    """축구 픽에 tv_world(전체)와 tv(앞의 show 개 한 줄, 예: '영국 Sky Sports Main Event · 미국 NBC')를 붙인다. 반환: 붙인 개수."""
    try:
        cache = json.load(io.open(CACHE, encoding="utf-8"))
    except Exception:
        cache = {}
    now = datetime.now(timezone.utc)
    comp_html, done = {}, 0
    for ev in picks:
        if ev.get("path") in SEASON:              # 경기마다 안 바뀌는 시즌 중계권(F1 등)
            chosen = [{"country": c, "label": l, "ch": ch} for c, l, ch in SEASON[ev["path"]]]
            ev["tv_kr"], ev["tv_world"] = ev.get("tv", ""), chosen
            ev["tv"] = " · ".join(f"{x['label']} {x['ch']}" for x in chosen[:show])
            done += 1
            continue
        if ev.get("path") not in COMP or not ev.get("home") or not ev.get("away"):
            continue
        c = cache.get(ev["id"])
        fresh = c and now - datetime.fromisoformat(c["at"]) < TTL
        if not fresh:
            comp, country = COMP[ev["path"]]
            pages = []
            for side in ("home", "away"):
                name = ev[side]["name"]
                tp = TEAM_SLUG.get(name) or (f"{country}/{_slug(name)}" if country else None)
                if tp:
                    pages.append((_get(f"{BASE}/teams/{tp}/"), True))
                    if _find(ev, pages[-1:]):
                        break
            href = _find(ev, pages)
            if not href:
                if comp not in comp_html:
                    comp_html[comp] = _get(BASE + comp)
                href = _find(ev, [(comp_html[comp], False)])
            table = _channels(_get(BASE + href)) if href else {}
            c = {"at": now.isoformat(), "url": BASE + href if href else None,
                 "table": {k: v for k, v in table.items() if k in {o[0] for o in ORDER} | {"Spain", "Germany"} | set(ARAB)}}
            if href or not cache.get(ev["id"]):
                cache[ev["id"]] = c
            else:
                c = cache[ev["id"]]
        chosen = pick_channels(ev["path"], c.get("table") or {}, (ev["home"]["name"], ev["away"]["name"]))
        if chosen:
            ev["tv_kr"] = ev.get("tv", "")
            ev["tv_world"] = chosen
            ev["tv"] = " · ".join(f"{x['label']} {x['ch']}" for x in chosen[:show])
            ev["tv_src"] = c.get("url")
            done += 1
    # 오래된 캐시(2주) 정리
    cut = now - timedelta(days=14)
    cache = {k: v for k, v in cache.items() if datetime.fromisoformat(v["at"]) >= cut}
    with io.open(CACHE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    return done
