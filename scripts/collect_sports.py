"""Sports UptoDate — 이번 주 볼 경기 고르기 + 지난 경기 결과.

출처: ESPN 공개 site API (키 없음, 비공식 — 실패하면 지난 결과를 그대로 둔다).
흐름: 리그별 scoreboard(어제~+7일) → 1차 점수(관심 리그·한국 선수·포스트시즌·시간대) →
      상위 후보만 summary 로 순위·최근 5경기·상대전적·주요 선수·결장자·승리 확률을 붙여 2차 점수 →
      주간 픽(최대 10, 리그당 3) 고정 → 끝난 픽엔 결과(스코어·골·한국 선수 기록) →
      data/sports.json + data/sports.ics (캘린더 구독).
KBO·LCK 는 반자동: data/sports_manual.json 에 편집 루틴이 확정 일정만 넣는다(여기선 그대로 싣기만).
"""
import io, json, os, re, sys, time, urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
KST = timezone(timedelta(hours=9))
UA = {"User-Agent": "Mozilla/5.0"}   # ESPN 방화벽이 봇 이름 UA 는 403 으로 막는다
API = "https://site.api.espn.com/apis/site/v2/sports/"


def load(p, d=None):
    try:
        return json.load(io.open(p, encoding="utf-8"))
    except Exception:
        return d


def get(url, tries=3):
    for i in range(tries):
        try:
            r = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30))
            time.sleep(0.8)                      # ESPN 은 짧은 시간에 몰아 부르면 403 으로 잠시 막는다
            return r
        except Exception as e:
            err = e
            time.sleep(20 if "403" in str(e) else 2)
    print("  ! 실패", url[:110], err, file=sys.stderr)
    return None


CFG = load(os.path.join(DATA, "sports_ko.json"), {})
TEAMS = CFG.get("teams", {})
PLAYERS = CFG.get("players", [])
DERBIES = [set(d) for d in CFG.get("derbies", [])]
FOLLOW = CFG.get("follow", [])
SPORT = {"soccer": "soccer", "baseball": "baseball", "basketball": "basketball", "racing": "f1",
         "tennis": "tennis", "golf": "golf", "mma": "mma", "football": "nfl"}


def sport_of(path):
    return SPORT.get(path.split("/")[0], "etc")


def ko(name):
    return TEAMS.get(name, name)


def kst(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(KST)


# ── 1. 후보 모으기 ──
def scoreboard(path, days):
    """리그당 달 단위로 한 번씩(dates=YYYYMM) — 날짜별로 부르면 열흘에 리그 20개 × 10번이라 ESPN 이 막는다."""
    out = []
    for month in sorted({d[:6] for d in days}):
        d = get(API + path + "/scoreboard?dates=%s&limit=500" % month) or get(API + path + "/scoreboard?dates=" + month)
        for e in (d or {}).get("events", []):
            if days[0] <= kst(e["date"]).strftime("%Y%m%d") <= days[-1]:
                out.append(e)
    seen, uniq = set(), []
    for e in out:
        if e["id"] not in seen:
            seen.add(e["id"]); uniq.append(e)
    return uniq


def base_event(path, e):
    """종목 공통 뼈대. 팀 경기는 home/away, F1·골프·UFC 는 단일 이벤트."""
    lg = CFG["leagues"][path]
    comp = (e.get("competitions") or [{}])[0]
    st = e.get("status", {}).get("type", {})
    ev = {"id": e["id"], "path": path, "league": lg["ko"], "name": e.get("name"), "date": e["date"],
          "kst": kst(e["date"]).strftime("%Y-%m-%d %H:%M"), "state": st.get("state"), "status": st.get("description"),
          "note": " / ".join(n.get("headline", "") for n in comp.get("notes", []) if n.get("headline")),
          "venue": (comp.get("venue") or {}).get("fullName", ""), "tv": lg.get("tv", ""), "sport": sport_of(path)}
    if path == "racing/f1":                      # 결승 세션 시각이 이벤트 시각이다
        race = [c for c in e.get("competitions", []) if (c.get("type") or {}).get("abbreviation") == "Race"]
        if race:
            ev["date"] = race[0]["date"]; ev["kst"] = kst(race[0]["date"]).strftime("%Y-%m-%d %H:%M")
            ev["sessions"] = [{"name": (c.get("type") or {}).get("abbreviation"), "date": c["date"],
                               "kst": kst(c["date"]).strftime("%Y-%m-%d %H:%M")} for c in e.get("competitions", [])]
            rs = race[0].get("status", {}).get("type", {})
            ev["state"], ev["status"] = rs.get("state", ev["state"]), rs.get("description", ev["status"])
            if ev["state"] == "post":                # 결과 = 포디움(1~3위)과 10위까지
                order = sorted(race[0].get("competitors", []), key=lambda c: c.get("order", 99))
                ev["detail"] = {"result": {"podium": [c.get("athlete", {}).get("displayName") for c in order[:3]],
                                           "top10": [c.get("athlete", {}).get("displayName") for c in order[:10]]}}
        ev["title"] = gp_ko(e.get("name") or "")
        return ev
    if path.startswith("golf/") or path.startswith("mma/"):
        ev["title"] = e.get("name")
        if path.startswith("mma/") and e.get("competitions"):
            main = e["competitions"][-1]
            ev["main"] = " vs ".join(c.get("athlete", {}).get("displayName", "") for c in main.get("competitors", []))
            ev["date"] = main.get("date", ev["date"]); ev["kst"] = kst(ev["date"]).strftime("%Y-%m-%d %H:%M")
        return ev
    teams = {}
    for c in comp.get("competitors", []):
        t = c.get("team", {})
        rec = [r.get("summary") for r in c.get("records", []) or [] if r.get("type") in ("total", "ytd")]
        teams[c.get("homeAway")] = {"name": t.get("displayName"), "ko": ko(t.get("displayName")), "abbr": t.get("abbreviation"),
                                    "id": t.get("id"), "score": c.get("score"), "winner": c.get("winner"),
                                    "record": rec[0] if rec else ""}
    ev["home"], ev["away"] = teams.get("home"), teams.get("away")
    if ev["home"] and ev["away"]:
        ev["title"] = "%s vs %s" % (ev["home"]["ko"], ev["away"]["ko"])
    return ev


GP = {"Singapore": "싱가포르", "United States": "미국", "Mexico City": "멕시코시티", "Mexican": "멕시코", "São Paulo": "상파울루", "Brazilian": "브라질",
      "Las Vegas": "라스베이거스", "Qatar": "카타르", "Abu Dhabi": "아부다비", "Japanese": "일본", "Azerbaijan": "아제르바이잔", "Italian": "이탈리아",
      "Dutch": "네덜란드", "Hungarian": "헝가리", "Belgian": "벨기에", "British": "영국", "Austrian": "오스트리아", "Spanish": "스페인", "Monaco": "모나코",
      "Canadian": "캐나다", "Miami": "마이애미", "Saudi Arabian": "사우디아라비아", "Bahrain": "바레인", "Chinese": "중국", "Australian": "호주", "Madrid": "마드리드"}


def gp_ko(name):
    """'Singapore Airlines Singapore Grand Prix' → '싱가포르 그랑프리' (스폰서 이름은 뗀다)."""
    for en, k in GP.items():
        if re.search(en + r" Grand Prix", name):
            return k + " 그랑프리"
    return name


# ── 테니스: 대회 안의 경기 중 챙기는 선수 경기 + 마스터스·메이저 4강·결승 ──
ROUND = {"Round 1": "1회전", "Round 2": "2회전", "Round 3": "3회전", "Round 4": "4회전", "Round of 16": "16강",
         "Quarterfinal": "8강", "Quarterfinals": "8강", "Semifinal": "4강", "Semifinals": "4강", "Final": "결승"}


def tennis_ko(name):
    for en, k in CFG.get("tennis_names", {}).items():
        if name and en in name:
            return k
    return name


def tourney_ko(name):
    n = re.sub(r"^(Rolex|Nitto|BNP Paribas|Mutua|National Bank)\s+", "", name or "")
    for en, k in CFG.get("tournaments", {}).items():
        if en in n:
            return k
    return n


def tennis_events(path, days):
    d = get(API + path + "/scoreboard")
    out = []
    follow = [f for f in FOLLOW if f.get("path") == path]
    for t in (d or {}).get("events", []):
        tko = tourney_ko(t.get("name"))
        for g in t.get("groupings", []):
            if "Singles" not in ((g.get("grouping") or {}).get("displayName") or ""):
                continue
            for c in g.get("competitions", []):
                if not (days[0] <= kst(c["date"]).strftime("%Y%m%d") <= days[-1]):
                    continue
                names = [(x.get("athlete") or {}).get("displayName") or "" for x in c.get("competitors", [])]
                rnd = (c.get("round") or {}).get("displayName") or ""
                fol = [f for f in follow if any(f["en"] in n for n in names)]
                big = rnd in ("Final", "Semifinal", "Semifinals") and ("Masters" in (t.get("name") or "") or t.get("major"))
                if not (fol or big):
                    continue
                st = c.get("status", {}).get("type", {})
                ev = {"id": "ten-" + str(c["id"]), "path": path, "sport": "tennis",
                      "league": "%s %s" % (CFG["leagues"][path]["ko"], tko),
                      "name": " vs ".join(names), "title": " vs ".join(tennis_ko(n) for n in names), "date": c["date"],
                      "kst": kst(c["date"]).strftime("%Y-%m-%d %H:%M"), "state": st.get("state"), "status": st.get("description"),
                      "note": ROUND.get(rnd, rnd), "venue": (t.get("venue") or {}).get("fullName", ""),
                      "tv": CFG["leagues"][path].get("tv", ""), "time_tbc": True}
                if fol:
                    ev["follow"] = fol[0]
                if ev["state"] == "post":
                    cs = c.get("competitors", [])
                    w = [x for x in cs if x.get("winner")]
                    lose = [x for x in cs if not x.get("winner")]
                    sets = ""
                    if w and lose:
                        sets = " ".join("%d-%d" % (a.get("value", 0), b.get("value", 0))
                                        for a, b in zip(w[0].get("linescores", []), lose[0].get("linescores", [])))
                    ev["detail"] = {"result": {"score": "%s 승 %s" % (tennis_ko((w[0].get("athlete") or {}).get("displayName")) if w else "", sets)}}
                out.append(ev)
    return out


def korean_in(ev):
    names = {(ev.get("home") or {}).get("name"), (ev.get("away") or {}).get("name")}
    return [p for p in PLAYERS if p["team"] in names]


def first_score(ev):
    """요약을 보기 전 1차 점수 — 관심 리그·한국 선수·포스트시즌·시간대."""
    lg = CFG["leagues"][ev["path"]]
    s, why = lg["w"], []
    fol = ev.get("follow") or next((f for f in FOLLOW if f.get("kind") == "team" and f["name"] in
                                    {(ev.get("home") or {}).get("name"), (ev.get("away") or {}).get("name")}), None)
    if fol:                                      # 사용자가 챙기는 선수·팀 — F1·알카라스는 축구만큼 중요하다
        s += fol["w"]; why.append(fol["ko"] + " 경기"); ev["follow"] = fol
    ks = korean_in(ev)
    if ks:                                       # 핵심 선수(tier 1)는 더 크게 — 손흥민 경기가 튀르키예 리그 경기보다 아래로 가면 안 된다
        s += max(40 if p.get("tier") == 1 else 24 for p in ks) + 6 * (len(ks) - 1)
        why.append(" · ".join(p["ko"] for p in ks) + " 출전 팀")
        side = "home" if (ev.get("home") or {}).get("name") == ks[0]["team"] else "away"     # 판 칩: '김민재 vs 아우크스부르크'
        ev["kp"] = {"ko": ks[0]["ko"], "opp": (ev.get("away" if side == "home" else "home") or {}).get("ko")}
    note = ev.get("note") or ""
    for pat, pts, label in [(r"World Series", 45, "월드시리즈"), (r"ALCS|NLCS|LCS", 32, "리그 챔피언십시리즈"),
                            (r"ALDS|NLDS|Division Series", 22, "디비전시리즈"), (r"Wild Card", 14, "와일드카드"),
                            (r"Final(?!s? ?Four)", 30, "결승"), (r"Semi", 20, "준결승"), (r"Quarter", 12, "8강")]:
        if re.search(pat, note):
            s += pts; why.append(label); break
    if re.search(r"Game [57]|If Necessary", note):
        s += 6; why.append("시리즈 분수령")
    names = {(ev.get("home") or {}).get("name"), (ev.get("away") or {}).get("name")}
    if any(d <= names for d in DERBIES):
        s += 18; why.append("라이벌전")
    if ev["path"] == "racing/f1":
        s += 12; why.append("그랑프리 결승")
    h = int(ev["kst"][11:13])
    if 19 <= h <= 23:
        s += 8; why.append("한국 저녁 시간")
    elif 0 <= h <= 2:
        s += 4
    elif 3 <= h <= 6:
        s -= 2
    return s, why


# ── 2. 상세(summary) — 순위·폼·상대전적·주요 선수·결장자·확률 ──
def table_rows(summary):
    rows = []
    for g in ((summary.get("standings") or {}).get("groups") or []):
        for i, en in enumerate(((g.get("standings") or {}).get("entries") or []), 1):
            st = {x.get("name"): x.get("displayValue") for x in en.get("stats", [])}
            rows.append({"team": en.get("team"), "rank": st.get("rank") or str(i), "pts": st.get("points"),
                         "gp": st.get("gamesPlayed"), "w": st.get("wins"), "l": st.get("losses"),
                         "gb": st.get("gamesBehind"), "pct": st.get("winPercent")})
    return rows


def detail(ev):
    if ev.get("src"):                             # ESPN 밖 출처(U-23 대표)는 요약 API 가 없다
        return
    s = get(API + ev["path"] + "/summary?event=" + ev["id"])
    if not s:
        return
    d = {}
    rows = table_rows(s)
    by = {r["team"]: r for r in rows}
    for side in ("home", "away"):
        t = ev.get(side) or {}
        r = by.get(t.get("name"))
        if r:
            t["rank"], t["pts"] = r["rank"], r["pts"]
    lf = {}
    for g in s.get("lastFiveGames", []) or []:
        tid = (g.get("team") or {}).get("id")
        lf[tid] = "".join({"W": "승", "L": "패", "D": "무", "T": "무"}.get(x.get("gameResult"), "") for x in g.get("events", [])[:5])
    for side in ("home", "away"):
        t = ev.get(side) or {}
        if t.get("id") in lf:
            t["form"] = lf[t["id"]]
    for ss in s.get("seasonseries", []) or []:
        if ss.get("type") in ("head-to-head", "season") and ss.get("summary"):
            last = []
            for x in (ss.get("events") or [])[:3]:
                cs = x.get("competitors") or []
                if len(cs) == 2:
                    last.append({"date": (x.get("date") or "")[:10],
                                 "score": " ".join("%s %s" % (ko(c.get("team", {}).get("displayName") or c.get("team", {}).get("abbreviation")), c.get("score")) for c in cs)})
            d["h2h"] = {"summary": h2h_ko(ss["summary"], ev), "last": last}
            break
    leaders = []
    for blk in s.get("leaders", []) or []:
        team = ko((blk.get("team") or {}).get("displayName"))
        for cat in (blk.get("leaders") or [])[:2]:
            top = (cat.get("leaders") or [{}])[0]
            if top.get("athlete"):
                leaders.append({"team": team, "cat": cat.get("displayName"), "who": top["athlete"].get("displayName"),
                                "val": stat_ko(top.get("displayValue"))})
    d["leaders"] = leaders[:6]
    inj = []
    for blk in s.get("injuries", []) or []:
        team = ko((blk.get("team") or {}).get("displayName"))
        for x in (blk.get("injuries") or [])[:4]:
            a = x.get("athlete") or {}
            inj.append({"team": team, "who": a.get("displayName"), "status": x.get("status")})
    d["injuries"] = inj
    pr = s.get("predictor") or {}
    if pr.get("homeTeam"):
        d["predictor"] = {"home": pr["homeTeam"].get("gameProjection"), "away": pr["awayTeam"].get("gameProjection")}
    news = (s.get("news") or {}).get("articles") or []
    names = [x for x in ((ev.get("home") or {}).get("name"), (ev.get("away") or {}).get("name")) if x]
    hit = [a for a in news if any(n.split()[-1] in (a.get("headline") or "") for n in names)]
    d["preview"] = [{"title": a.get("headline"), "url": ((a.get("links") or {}).get("web") or {}).get("href")} for a in hit[:2]]
    if ev["state"] == "post":
        d["result"] = result(ev, s)
    ev["detail"] = d
    return s


def h2h_ko(t, ev):
    """'MNC leads series 3-2' → '맨시티 우세 (3승 2패)', 'Series tied 1-1' → '동률 (1승 1패)'."""
    ab = {(x or {}).get("abbr"): (x or {}).get("ko") for x in (ev.get("home"), ev.get("away"))}
    m = re.match(r"(\w+) leads series (\d+)-(\d+)(?:-(\d+))?", t or "")
    if m:
        w, l, dr = m.group(2), m.group(3), m.group(4)
        return "%s 우세 (%s승 %s%s패)" % (ab.get(m.group(1), m.group(1)), w, (dr + "무 ") if dr else "", l) if not dr else                "%s 우세 (%s승 %s무 %s패)" % (ab.get(m.group(1), m.group(1)), w, l, dr)
    m = re.match(r"Series tied (\d+)-(\d+)", t or "")
    if m:
        return "동률 (%s승 %s패)" % (m.group(1), m.group(2))
    return t


def stat_ko(v):
    for en, k in (("Matches", "경기"), ("Goals", "골"), ("Assists", "도움"), ("Appearances", "출전"), ("Clean Sheets", "무실점")):
        v = re.sub(en + r":\s*(\d+)", lambda m: m.group(1) + k, v or "")
    return v.replace(",", " ·")


def second_score(ev):
    """요약을 본 뒤 가산 — 상위권 맞대결·박빙."""
    add, why = 0, []
    h, a = ev.get("home") or {}, ev.get("away") or {}
    try:
        rh, ra = int(h.get("rank") or 99), int(a.get("rank") or 99)
        if rh <= 4 and ra <= 4:
            add += 20; why.append("%s위·%s위 상위권 맞대결" % (rh, ra))
        elif rh <= 6 and ra <= 6:
            add += 10; why.append("상위권 맞대결")
        if min(rh, ra) == 1:
            add += 6; why.append("선두 경기")
    except ValueError:
        pass
    pr = (ev.get("detail") or {}).get("predictor")
    if pr:
        try:
            if abs(float(pr["home"]) - 50) <= 7:
                add += 5; why.append("박빙 예상")
        except (TypeError, ValueError):
            pass
    return add, why


# ── 3. 결과 ──
def result(ev, s):
    out = {"score": "%s %s : %s %s" % ((ev.get("home") or {}).get("ko"), (ev.get("home") or {}).get("score"),
                                        (ev.get("away") or {}).get("score"), (ev.get("away") or {}).get("ko"))}
    goals = []
    for k in s.get("keyEvents", []) or []:
        tt = ((k.get("type") or {}).get("text") or "")
        if "Goal" in tt or "Penalty - Scored" in tt:
            goals.append({"min": (k.get("clock") or {}).get("displayValue"), "text": (k.get("text") or "")[:140]})
    out["goals"] = goals
    ks = []
    want = {p["en"].lower(): p["ko"] for p in PLAYERS}
    for r in s.get("rosters", []) or []:
        for p in r.get("roster", []) or []:
            nm = (p.get("athlete") or {}).get("displayName", "")
            if nm.lower() in want:
                st = {x.get("name"): x.get("displayValue") for x in p.get("stats", []) or []}
                ks.append({"ko": want[nm.lower()], "starter": p.get("starter"), "sub_in": p.get("subbedIn"),
                           "goals": st.get("totalGoals"), "assists": st.get("goalAssists"), "shots": st.get("totalShots"),
                           "played": (p.get("starter") or p.get("subbedIn"))})
    # 야구: 박스스코어 타자 기록
    for tb in ((s.get("boxscore") or {}).get("players") or []):
        for grp in tb.get("statistics", []) or []:
            labels = grp.get("labels") or []
            for a in grp.get("athletes", []) or []:
                nm = (a.get("athlete") or {}).get("displayName", "")
                if nm.lower() in want:
                    ks.append({"ko": want[nm.lower()], "line": dict(zip(labels, a.get("stats") or [])), "type": grp.get("type") or grp.get("name")})
    out["korean"] = ks
    vids = [v for v in (s.get("videos") or []) if v.get("headline")]
    if vids:
        out["video"] = {"title": vids[0]["headline"], "url": ((vids[0].get("links") or {}).get("web") or {}).get("href")}
    return out


# ── 4. 주간 픽 ──
def stars(score):
    return 3 if score >= 70 else 2 if score >= 50 else 1


def main():
    now = datetime.now(KST)
    days = [(now + timedelta(days=d)).strftime("%Y%m%d") for d in range(-2, 8)]
    prev = load(os.path.join(DATA, "sports.json"), {}) or {}
    cand = []
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    for path in CFG["leagues"]:
        if CFG["leagues"][path].get("src") == "lst":   # ESPN 에 없는 대회(U-23 대표) — 아래에서 따로
            continue
        if path.startswith("tennis/"):
            for ev in tennis_events(path, days):
                ev["score"], ev["why"] = first_score(ev)
                cand.append(ev)
            print("%-24s %d (챙기는 선수·큰 경기)" % (path, len([c for c in cand if c["path"] == path])))
            continue
        evs = scoreboard(path, days)
        print("%-24s %d" % (path, len(evs)))
        for e in evs:
            try:
                ev = base_event(path, e)
            except Exception as x:
                print("  ! 건너뜀", path, e.get("id"), x, file=sys.stderr); continue
            sc, why = first_score(ev)
            ev["score"], ev["why"] = sc, why
            cand.append(ev)
    try:                                          # 올림픽·아시안게임 대표(U-23) — 사용자 2026-10-11
        import korea_u23
        u23 = korea_u23.events(days)
        for ev in u23:
            ev["score"], ev["why"] = first_score(ev)
            cand.append(ev)
        print("%-24s %d" % ("U-23 대표(LiveSoccerTV)", len(u23)))
    except Exception as x:
        print("  ! U-23 대표 건너뜀", x, file=sys.stderr)
    # 주 = 오늘 0시 ~ 7일 뒤, 결과 = 지난 48시간
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    upcoming = [e for e in cand if e["state"] != "post" and start <= kst(e["date"]) < start + timedelta(days=7)]
    upcoming.sort(key=lambda e: -e["score"])
    pool = upcoming[:30]
    for ev in pool:
        if ev.get("home"):
            detail(ev)
            add, why = second_score(ev)
            ev["score"] += add; ev["why"] += why
    pool.sort(key=lambda e: -e["score"])
    # 같은 대진(시리즈)은 한 칸 — 가장 이른 경기를 싣고 나머지는 '이후 일정'으로
    series = {}
    for ev in sorted(upcoming, key=lambda e: e["date"]):
        if ev.get("home") and ev.get("away"):
            series.setdefault(frozenset((ev["home"]["name"], ev["away"]["name"])), []).append(ev)
    picks, per, seen, per_player, per_sport = [], {}, set(), {}, {}
    must = [e for e in pool if e["path"] in CFG.get("must", {}) or e.get("follow")]
    for ev in must + [e for e in pool if e not in must]:
        key = frozenset(((ev.get("home") or {}).get("name"), (ev.get("away") or {}).get("name"))) if ev.get("home") else ev["id"]
        ks = [p["ko"] for p in korean_in(ev)]
        cap = CFG.get("sport_cap", {}).get(ev.get("sport"))
        pcap = CFG.get("path_cap", {}).get(ev["path"], 3)
        if key in seen or per.get(ev["path"], 0) >= pcap or any(per_player.get(k, 0) >= 1 for k in ks) \
                or (cap and per_sport.get(ev.get("sport"), 0) >= cap):
            continue
        if ev.get("home"):
            games = series.get(key, [ev])
            ev = games[0] if games[0] is not ev and games[0] in pool else ev
            if len(games) > 1:
                ev["series"] = [{"kst": g["kst"], "note": g.get("note")} for g in games]
        seen.add(key); per[ev["path"]] = per.get(ev["path"], 0) + 1
        per_sport[ev.get("sport")] = per_sport.get(ev.get("sport"), 0) + 1
        for k in ks:
            per_player[k] = per_player.get(k, 0) + 1
        picks.append(ev)
        if len(picks) >= 10:
            break
    for i, ev in enumerate(sorted(picks, key=lambda e: -e["score"])):   # 별은 이번 주 안의 순위로
        ev["stars"] = 3 if i < 3 else 2 if i < 7 else 1
        if ev["path"] == "racing/f1":            # F1 결승은 늘 ★★★ (사용자: '되게 중요')
            ev["stars"] = 3
        elif (ev.get("follow") or {}).get("kind") == "athlete":
            ev["stars"] = max(ev["stars"], 3 if ev.get("note") in ("8강", "4강", "결승") else 2)
    unknown = sorted({t["name"] for e in picks for t in (e.get("home"), e.get("away")) if t and t["ko"] == t["name"] and not t["name"].isupper()})
    if unknown:
        print("  한국어 이름 없음 →", ", ".join(unknown))
    picks.sort(key=lambda e: e["date"])
    try:                                          # 축구는 해외 현지 채널로(사용자 2026-10-11) — 실패하면 리그 기본 문구
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import world_tv
        print("현지 중계 %d건" % world_tv.attach(picks))
    except Exception as x:
        print("  ! 현지 중계 건너뜀", x, file=sys.stderr)
    # 지난 픽의 결과 — 직전 실행에서 골랐던 경기 중 끝난 것
    done = []
    old = {p["id"]: p for p in (prev.get("picks") or []) + (prev.get("results") or [])}
    byid = {e["id"]: e for e in cand}
    for pid, p in old.items():
        e = byid.get(pid)
        if e and e["state"] == "post" and kst(e["date"]) >= now - timedelta(hours=60):
            e.update({k: p.get(k) for k in ("why", "stars", "score", "follow") if p.get(k) is not None})
            if e.get("home"):
                detail(e)
            done.append(e)
    done.sort(key=lambda e: e["date"], reverse=True)
    manual = load(os.path.join(DATA, "sports_manual.json"), {}) or {}
    out = {"updated": now.strftime("%Y-%m-%d %H:%M KST"), "source": "ESPN (비공식 공개 API)",
           "picks": picks, "results": done, "manual": manual.get("events", [])}
    if not picks and prev.get("picks"):
        print("! 픽 0건 — 지난 데이터 유지", file=sys.stderr); return
    io.open(os.path.join(DATA, "sports.json"), "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1))
    write_ics(picks + [m for m in manual.get("events", []) if m.get("verified")])
    print("픽 %d · 결과 %d" % (len(picks), len(done)))
    for p in picks:
        print(" ", "★" * p["stars"], p["kst"], p["league"], p.get("title"), "|", " · ".join(p["why"]))


def write_ics(evs):
    """캘린더 구독용 — 구글·애플 캘린더에 URL 로 한 번 구독하면 매주 갱신된다."""
    L = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//sharktalk//Sports UptoDate//KO", "X-WR-CALNAME:Sports UptoDate 볼 경기",
         "X-WR-TIMEZONE:Asia/Seoul", "CALSCALE:GREGORIAN", "METHOD:PUBLISH"]
    for e in evs:
        try:
            t = datetime.fromisoformat(e["date"].replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            continue
        dur = 150 if not e.get("path", "").startswith("golf") else 300
        title = "%s %s" % (e.get("league", ""), e.get("title") or e.get("name") or "")
        desc = " · ".join(e.get("why") or []) + ("\\n" + e["tv"] if e.get("tv") else "")
        L += ["BEGIN:VEVENT", "UID:%s@sports.sharktalk" % e.get("id"), "DTSTAMP:" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
              "DTSTART:" + t.strftime("%Y%m%dT%H%M%SZ"), "DTEND:" + (t + timedelta(minutes=dur)).strftime("%Y%m%dT%H%M%SZ"),
              "SUMMARY:" + title.replace(",", "\\,"), "DESCRIPTION:" + desc.replace(",", "\\,"),
              "BEGIN:VALARM", "TRIGGER:-PT30M", "ACTION:DISPLAY", "DESCRIPTION:" + title.replace(",", "\\,"), "END:VALARM", "END:VEVENT"]
    L.append("END:VCALENDAR")
    io.open(os.path.join(DATA, "sports.ics"), "w", encoding="utf-8", newline="").write("\r\n".join(L) + "\r\n")


if __name__ == "__main__":
    main()
