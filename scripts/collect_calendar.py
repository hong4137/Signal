"""경제 일정 원자료 수집 — LLM 없이 API·공식 일정표만 돈다. GitHub Actions 03:00 KST.

결과 data/calendar_raw.json 을 06:20 경제 일정 루틴(LLM)이 읽어 고르고 다듬는다.
키가 없는 출처는 건너뛰고 sources 에 이유를 남긴다.

  출처                    키(환경변수)                       받는 것
  FRED 발표일             FRED_API_KEY                       미국 지표 예정 발표일
  Finnhub 실적 캘린더     FINNHUB_API_KEY                    미국 실적일·예상치 (관심 종목)
  오픈DART 공시 목록      DART_API_KEY                       관심 종목의 IR 개최·실적 예고·잠정실적 공시
  네이버 뉴스 검색        NAVER_CLIENT_ID / NAVER_CLIENT_SECRET   계획표의 한국어 검색어 → 최근 기사
  연준 FOMC 일정표        (없음)                             FOMC 회의 날짜
  규칙 일정               (없음)                             옵션 만기·수출입 발표일 등 (calendar_rules.py)

  python3 scripts/collect_calendar.py [YYYY-MM-DD]
"""
import html
import io
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from email.utils import parsedate_to_datetime
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from calendar_rules import rules, et_to_kst  # noqa: E402

KST = timezone(timedelta(hours=9))
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DATA = os.path.join(ROOT, "data")
UA = "Mozilla/5.0 (DailyUptoDate calendar collector)"
DAYS = 7                       # 원자료는 지면 범위(5일)보다 넉넉히

# FRED 발표 이름 → (한국어 제목, 중요도, 미국 동부 발표 시각)
FRED_PICK = [
    ("Consumer Price Index", "미국 소비자물가(CPI)", 5, (8, 30)),
    ("Employment Situation", "미국 고용보고서(비농업 고용·실업률)", 5, (8, 30)),
    ("Producer Price Index", "미국 생산자물가(PPI)", 4, (8, 30)),
    ("Personal Income and Outlays", "미국 개인소득·지출(PCE 물가)", 5, (8, 30)),
    ("Gross Domestic Product", "미국 GDP", 4, (8, 30)),
    ("Advance Monthly Sales for Retail", "미국 소매판매", 4, (8, 30)),
    ("Job Openings and Labor Turnover", "미국 구인·이직(JOLTS)", 4, (10, 0)),
    ("Unemployment Insurance Weekly Claims", "미국 주간 신규 실업수당 청구", 3, (8, 30)),
    ("Industrial Production and Capacity", "미국 산업생산", 3, (9, 15)),
    ("Surveys of Consumers", "미국 미시간대 소비자심리", 3, (10, 0)),
    ("ISM Manufacturing", "미국 ISM 제조업 지수", 4, (10, 0)),
    ("New Residential Construction", "미국 주택착공", 3, (8, 30)),
]
# FRED 가 매일 갱신 자료로 싣는 것·같은 지표의 주별판은 뺀다 (FOMC 는 연준 일정표에서 받는다)
FRED_SKIP = re.compile(r"^State |FOMC", re.I)

# Finnhub — 관심 종목(미국 상장 심볼)
US_SYMBOLS = {
    "NVDA": "엔비디아", "TSM": "TSMC", "ASML": "ASML", "MSFT": "마이크로소프트", "AAPL": "애플",
    "GOOGL": "알파벳(구글)", "AMZN": "아마존", "META": "메타", "TSLA": "테슬라", "MU": "마이크론",
    "AVGO": "브로드컴", "NFLX": "넷플릭스", "AMD": "AMD", "INTC": "인텔", "QCOM": "퀄컴",
    "ORCL": "오라클", "IBM": "IBM", "ARM": "Arm", "SMCI": "슈퍼마이크로", "PLTR": "팔란티어",
    "JPM": "JP모건", "GS": "골드만삭스", "MS": "모건스탠리", "BAC": "뱅크오브아메리카",
    "C": "씨티그룹", "WFC": "웰스파고",
}
US_HOUR = {"bmo": "장 시작 전", "amc": "장 마감 후", "dmh": "장중"}

# DART — 관심 종목 이름과 공시 제목 패턴
DART_PAT = re.compile(r"기업설명회|결산실적공시\s*예고|영업\(잠정\)실적|연결재무제표기준영업\(잠정\)실적")


def get(url, headers=None, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def kst_of(d, hm):
    return et_to_kst(d, hm[0], hm[1]) if hm else (d, None)


# ── 출처별 수집 ──
def fred(d0, d1):
    key = os.environ.get("FRED_API_KEY")
    if not key:
        return None, "FRED_API_KEY 없음"
    q = urllib.parse.urlencode({"api_key": key, "file_type": "json", "realtime_start": d0.isoformat(),
                                "realtime_end": d1.isoformat(), "include_release_dates_with_no_data": "true",
                                "sort_order": "asc", "limit": 1000})
    rows = json.loads(get("https://api.stlouisfed.org/fred/releases/dates?" + q)).get("release_dates", [])
    out, seen = [], set()
    for r in rows:
        name = r.get("release_name", "")
        if FRED_SKIP.search(name):
            continue
        for pat, ko, imp, hm in FRED_PICK:
            if pat.lower() in name.lower():
                d = date.fromisoformat(r["date"])
                kd, kt = kst_of(d, hm)
                k = (ko, kd)
                if k in seen:
                    break
                seen.add(k)
                out.append({"src": "FRED", "kind": "event", "date": kd.isoformat(), "time": kt,
                            "region": "US", "title": ko, "importance": imp,
                            "detail": "%s (미 동부 %s %02d:%02d 관행 시각)" % (name, d.isoformat(), hm[0], hm[1]),
                            "url": "https://fred.stlouisfed.org/release?rid=%s" % r.get("release_id")})
                break
    return out, None


def finnhub(d0, d1):
    key = os.environ.get("FINNHUB_API_KEY")
    if not key:
        return None, "FINNHUB_API_KEY 없음"
    q = urllib.parse.urlencode({"from": d0.isoformat(), "to": d1.isoformat(), "token": key})
    rows = json.loads(get("https://finnhub.io/api/v1/calendar/earnings?" + q)).get("earningsCalendar", [])
    out = []
    for r in rows:
        sym = r.get("symbol")
        if sym not in US_SYMBOLS:
            continue
        d = date.fromisoformat(r["date"])
        hour = r.get("hour") or ""
        # 장 전(bmo)은 대개 07:00 ET 전후, 장 후(amc)는 16:05 ET 전후 — 정확한 시각은 회사 공지로
        kd, kt = kst_of(d, (7, 0) if hour == "bmo" else (16, 5) if hour == "amc" else None)
        est = []
        if r.get("epsEstimate") is not None:
            est.append("EPS 예상 %.2f달러" % r["epsEstimate"])
        if r.get("revenueEstimate"):
            est.append("매출 예상 %.1f억달러" % (r["revenueEstimate"] / 1e8))
        out.append({"src": "Finnhub", "kind": "event", "date": kd.isoformat(), "time": kt, "region": "US",
                    "title": "%s %d분기 실적" % (US_SYMBOLS[sym], r.get("quarter") or 0),
                    "importance": 5 if sym == "NVDA" else 4,
                    "detail": "%s · %s · %s" % (sym, US_HOUR.get(hour, hour or "시각 미정"), " · ".join(est) or "예상치 없음"),
                    "consensus": " · ".join(est) or None, "url": "https://finnhub.io/"})
    return out, None


def dart(d0, watch):
    key = os.environ.get("DART_API_KEY")
    if not key:
        return None, "DART_API_KEY 없음"
    out = []
    for page in range(1, 6):
        q = urllib.parse.urlencode({"crtfc_key": key, "bgn_de": (d0 - timedelta(days=14)).strftime("%Y%m%d"),
                                    "end_de": d0.strftime("%Y%m%d"), "corp_cls": "Y",
                                    "page_no": page, "page_count": 100})
        d = json.loads(get("https://opendart.fss.or.kr/api/list.json?" + q))
        if d.get("status") not in ("000", "013"):
            return out, "DART %s %s" % (d.get("status"), d.get("message"))
        for r in d.get("list", []):
            nm = r.get("report_nm", "")
            if r.get("corp_name") in watch and DART_PAT.search(nm):
                out.append({"src": "DART", "kind": "filing", "date": None, "filed": r.get("rcept_dt"),
                            "region": "KR", "title": "%s — %s" % (r["corp_name"], re.sub(r"\s+", " ", nm).strip()),
                            "detail": "공시일 %s. 행사·발표 날짜는 공시 본문에 있다" % r.get("rcept_dt"),
                            "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=%s" % r.get("rcept_no")})
        if page >= int(d.get("total_page") or 1):
            break
    return out, None


def naver_keys():
    cid, sec = os.environ.get("NAVER_CLIENT_ID"), os.environ.get("NAVER_CLIENT_SECRET")
    if cid and sec:
        return cid, sec
    try:                                   # 로컬 개발용 — enrich.py 와 같은 키 파일
        toks = re.findall(r"[A-Za-z0-9_]{6,40}", io.open(r"d:/개발/네이버 API.txt", encoding="utf-8",
                                                          errors="ignore").read())
        return (toks[0], toks[1]) if len(toks) >= 2 else (None, None)
    except Exception:
        return None, None


def naver(queries, d0):
    cid, sec = naver_keys()
    if not (cid and sec):
        return None, "NAVER 키 없음", {}
    out, per, seen = [], {}, set()
    since = d0 - timedelta(days=7)
    for qid, q in queries:
        u = "https://openapi.naver.com/v1/search/news.json?" + urllib.parse.urlencode(
            {"query": q, "display": 30, "sort": "date"})   # 최신순, 최근 7일만 남긴다
        try:
            items = json.loads(get(u, headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec})).get("items", [])
        except Exception as e:
            per[qid] = "실패 %s" % e
            continue
        n = 0
        for x in items:
            try:
                pub = parsedate_to_datetime(x["pubDate"]).astimezone(KST)     # 로캘과 무관
            except Exception:
                continue
            if pub.date() < since:
                continue
            url = x.get("originallink") or x.get("link")
            if url in seen:
                continue
            seen.add(url)
            n += 1
            clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()
            out.append({"src": "네이버뉴스", "kind": "news", "query_id": qid, "date": None,
                        "published": pub.strftime("%Y-%m-%d %H:%M"), "region": "KR",
                        "title": clean(x.get("title")), "detail": clean(x.get("description")), "url": url})
        per[qid] = n
    return out, None, per


def fomc(d0, d1):
    page = get("https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm")
    out = []
    for y in (d0.year, d1.year) if d0.year != d1.year else (d0.year,):
        m = re.search(r"%d FOMC Meetings(.*?)(?:\d{4} FOMC Meetings|$)" % y, page, re.S)
        if not m:
            continue
        for mon, days in re.findall(r'fomc-meeting__month[^>]*>\s*<strong>([A-Za-z/]+)</strong>.*?'
                                    r'fomc-meeting__date[^>]*>([^<]+)<', m.group(1), re.S):
            mon = mon.split("/")[-1]
            dd = re.findall(r"\d+", days)
            if not dd:
                continue
            try:
                last = datetime.strptime("%s %s %d" % (mon[:3], dd[-1], y), "%b %d %Y").date()
            except ValueError:
                continue
            kd, kt = kst_of(last, (14, 0))
            if d0 <= kd <= d1:
                out.append({"src": "Fed", "kind": "event", "date": kd.isoformat(), "time": kt, "region": "US",
                            "title": "FOMC 금리 결정", "importance": 5,
                            "detail": "FOMC %s %s (마지막 날 14:00 ET 성명)" % (mon, days.strip()),
                            "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"})
    return out, None


def main():
    d0 = date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else datetime.now(KST).date()
    d1 = d0 + timedelta(days=DAYS)
    pb = json.load(io.open(os.path.join(DATA, "calendar_playbook.json"), encoding="utf-8"))
    from calendar_queries import applies            # 계획표의 네이버 전용 검색어 중 오늘 시기에 맞는 것
    ko = [(x["id"], x["q"]) for x in pb.get("naver", []) if applies(x.get("when"), d0)]

    items, sources = [], {}

    def run(name, fn, *a):
        try:
            r = fn(*a)
            got, err = r[0], r[1]
            if got is None:
                sources[name] = {"ok": False, "skip": err}
                return r
            items.extend(got)
            sources[name] = {"ok": True, "count": len(got), **({"note": err} if err else {})}
            return r
        except Exception as e:
            sources[name] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            return None

    run("FRED", fred, d0, d1)
    run("Finnhub", finnhub, d0, d1)
    run("DART", dart, d0, set(pb["watch"]["kr"]))
    r = run("네이버뉴스", naver, ko, d0)
    if r and len(r) > 2:
        sources["네이버뉴스"]["per_query"] = r[2]
    run("FOMC", fomc, d0, d1)
    fixed = rules(d0, d0 + timedelta(days=5))

    out = {"generated_at": datetime.now(KST).isoformat(timespec="seconds"),
           "window": {"from": d0.isoformat(), "to": d1.isoformat()},
           "sources": sources, "rule_candidates": fixed,
           "events": sorted([x for x in items if x["kind"] == "event"], key=lambda x: (x["date"], x["time"] or "99")),
           "filings": [x for x in items if x["kind"] == "filing"],
           "news": [x for x in items if x["kind"] == "news"]}
    io.open(os.path.join(DATA, "calendar_raw.json"), "w", encoding="utf-8", newline="\n").write(
        json.dumps(out, ensure_ascii=False, indent=1) + "\n")
    print("경제 일정 원자료 %s~%s — 일정 %d · 공시 %d · 기사 %d" % (
        d0, d1, len(out["events"]), len(out["filings"]), len(out["news"])))
    for k, v in sources.items():
        print("  %s: %s" % (k, json.dumps(v, ensure_ascii=False)))


if __name__ == "__main__":
    main()
