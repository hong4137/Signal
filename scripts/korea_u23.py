"""대한민국 U-23 대표(올림픽·아시안게임 대표) 경기 — ESPN 에 없는 대회(아시안게임·U-23 아시안컵·U-23 친선)를 채운다.

사용자(2026-10-11): 국가대표에 더해 올림픽대표·아시안게임대표까지 넣자.
출처: LiveSoccerTV 팀 페이지 /teams/korea-republic/korea-republic-u23/ (일정·결과)
      + 경기 페이지(대회 이름 · 한국 중계 채널). world_tv 와 같은 UA·간격·robots 허용 경로만.
A대표는 ESPN(fifa.friendly·afc.asian.cup·fifa.olympics 등)에서 'South Korea' 로 잡고 sports_ko.json follow 로 챙긴다.
"""
import io, json, os, re
from datetime import datetime, timedelta, timezone
from html import unescape

from world_tv import BASE, _channels, _get

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, "data", "korea_u23_cache.json")
TEAM = "/teams/korea-republic/korea-republic-u23/"
PATH = "soccer/kor.u23"
KST = timezone(timedelta(hours=9))
SELF = "Korea Republic U23"

COMP_KO = {"Asian Games": "아시안게임", "AFC U23 Asian Cup": "U-23 아시안컵", "AFC U23 Asian Cup Qualifiers": "U-23 아시안컵 예선",
           "Olympic Games": "올림픽", "Olympics": "올림픽", "Friendlies": "친선", "International Friendlies": "친선",
           "Friendly": "친선", "U23 Friendlies": "친선"}
NATION_KO = {"Japan": "일본", "China PR": "중국", "China": "중국", "Vietnam": "베트남", "Thailand": "태국", "Australia": "호주",
             "Saudi Arabia": "사우디", "Qatar": "카타르", "Iran": "이란", "Iraq": "이라크", "Uzbekistan": "우즈베키스탄",
             "United Arab Emirates": "UAE", "Jordan": "요르단", "Indonesia": "인도네시아", "Malaysia": "말레이시아",
             "Kuwait": "쿠웨이트", "Bahrain": "바레인", "Oman": "오만", "Kyrgyzstan": "키르기스스탄", "Tajikistan": "타지키스탄",
             "Korea DPR": "북한", "India": "인도", "Hong Kong": "홍콩", "Philippines": "필리핀", "Singapore": "싱가포르",
             "Palestine": "팔레스타인", "Lebanon": "레바논", "Syria": "시리아", "Myanmar": "미얀마", "Chinese Taipei": "대만",
             "Korea Republic": "대한민국"}


def team_ko(name):
    base = re.sub(r"\s*U-?23$", "", name or "").strip()
    return NATION_KO.get(base, base) + " U-23"


def _rows(html):
    """팀 페이지 → [(경기 id, 킥오프 epoch, 제목, 주소, 점수 or None)]"""
    out = []
    for m in re.finditer(r"<tr id=\"(\d+)\" class=\"matchrow\".*?dv='(\d+)'.*?<a href=\"(/match/[^\"#]+)[^\"]*\" title=\"([^\"]+)\"[^>]*>(.*?)</a>",
                         html or "", re.S):
        sc = re.search(r"<score>\s*(\d+)\s*-\s*(\d+)\s*</score>", m.group(5))
        out.append((m.group(1), int(m.group(2)) // 1000, unescape(m.group(4)), m.group(3), (sc.group(1), sc.group(2)) if sc else None))
    return out


def events(days):
    """collect_sports 의 후보 형식으로 — days = ['YYYYMMDD', …] (어제~+7일)."""
    try:
        cache = json.load(io.open(CACHE, encoding="utf-8"))
    except Exception:
        cache = {}
    now = datetime.now(timezone.utc)
    out = []
    for fid, ts, title, href, score in _rows(_get(BASE + TEAM)):
        t = datetime.fromtimestamp(ts, timezone.utc)
        if not (days[0] <= t.astimezone(KST).strftime("%Y%m%d") <= days[-1]):
            continue
        home_n, _, away_n = title.partition(" vs ")
        c = cache.get(fid)
        if not c or now - datetime.fromisoformat(c["at"]) > timedelta(hours=12) or (score and not c.get("final")):
            page = _get(BASE + href) or ""
            comp = re.search(r"<div class='m-title'><a href=\"/competitions/[^\"]+\"[^>]*>([^<]+)</a>", page)
            kr = [re.sub(r"\s+Korea$", "", x) for x in _channels(page).get("Korea Republic", [])]   # 'KBS2 Korea' → 'KBS2'
            c = {"at": now.isoformat(), "comp": unescape(comp.group(1)).strip() if comp else "", "kr": kr, "final": bool(score)}
            cache[fid] = c
        comp_ko = COMP_KO.get(c["comp"], c["comp"] or "경기")
        home = {"name": home_n.strip(), "ko": team_ko(home_n), "abbr": None, "id": None, "score": score[0] if score else None,
                "winner": None, "record": ""}
        away = {"name": away_n.strip(), "ko": team_ko(away_n), "abbr": None, "id": None, "score": score[1] if score else None,
                "winner": None, "record": ""}
        if score:
            hs, as_ = int(score[0]), int(score[1])
            home["winner"], away["winner"] = hs > as_, as_ > hs
        ev = {"id": "lst" + fid, "src": "lst", "path": PATH, "league": "U-23 대표 " + comp_ko, "name": title,
              "date": t.strftime("%Y-%m-%dT%H:%MZ"), "kst": t.astimezone(KST).strftime("%Y-%m-%d %H:%M"),
              "state": "post" if score else ("in" if t <= now else "pre"), "status": "FT" if score else "",
              "note": "", "venue": "", "tv": " · ".join(c["kr"][:3]) or "국내 중계 미확인", "sport": "soccer",
              "home": home, "away": away, "title": "%s vs %s" % (home["ko"], away["ko"]), "tv_src": BASE + href}
        if score:
            ev["detail"] = {"result": {"score": "%s %s : %s %s" % (home["ko"], score[0], score[1], away["ko"]),
                                       "goals": [], "korean": []}}
        out.append(ev)
    cut = now - timedelta(days=30)
    cache = {k: v for k, v in cache.items() if datetime.fromisoformat(v["at"]) >= cut}
    with io.open(CACHE, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cache, f, ensure_ascii=False, indent=1)
    return out
