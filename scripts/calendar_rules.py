"""경제 일정 루틴용 — 날짜가 규칙으로 정해지는 일정 후보를 뽑는다.

검색으로 매번 찾을 필요 없는 것(옵션 만기, 수출입 발표일, 미국 주간 실업수당 등)을
계산해서 후보로 내놓는다. 후보일 뿐이다 — 루틴이 검색으로 날짜·시각을 확인한 것만 싣는다.
휴일 이동은 모른다: 주말에 걸리면 표시만 해 둔다.

사용: python3 scripts/calendar_rules.py [YYYY-MM-DD]   (기본 = 오늘 KST, 범위 = 오늘~5일 뒤)
"""
import json
import sys
from datetime import date, datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
DOW = "월화수목금토일"


def nth_weekday(y, m, wd, n):
    """y년 m월의 n번째 wd요일(월=0)."""
    d = date(y, m, 1)
    d += timedelta(days=(wd - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def us_dst(d):
    """미국 서머타임 — 3월 둘째 일요일 ~ 11월 첫째 일요일."""
    return nth_weekday(d.year, 3, 6, 2) <= d < nth_weekday(d.year, 11, 6, 1)


def et_to_kst(d, hh, mm):
    """미국 동부 시각 → 한국 날짜·시각."""
    off = 13 if us_dst(d) else 14
    t = datetime(d.year, d.month, d.day, hh, mm) + timedelta(hours=off)
    return t.date(), t.strftime("%H:%M")


def rules(d0, d5):
    out = []

    def add(d, time, region, title, imp, how):
        if d0 <= d <= d5:
            wk = "" if d.weekday() < 5 else " (주말 — 실제 발표일 확인)"
            out.append({"date": d.isoformat(), "time": time, "region": region, "title": title,
                        "importance": imp, "rule": how + wk})

    months = sorted({(d0 + timedelta(days=i)).replace(day=1) for i in range((d5 - d0).days + 1)})
    for m in months:
        y, mo = m.year, m.month
        q = mo in (3, 6, 9, 12)
        add(nth_weekday(y, mo, 3, 2), "15:20", "KR",
            ("주가지수 선물·옵션 동시만기" if q else "주가지수 옵션 만기"), 4 if q else 3,
            "매월 둘째 목요일 (3·6·9·12월은 선물까지 동시만기)")
        add(date(y, mo, 1), "09:00", "KR", "%d월 수출입 동향(산업부)" % ((mo - 2) % 12 + 1), 4,
            "매월 1일 전월치 — 주말에도 발표하는 관행")
        add(date(y, mo, 11), "09:00", "KR", "%d월 1~10일 수출입 잠정치(관세청)" % mo, 4,
            "매월 11일 — 휴일이면 다음 평일일 수 있음")
        add(date(y, mo, 21), "09:00", "KR", "%d월 1~20일 수출입 잠정치(관세청)" % mo, 4,
            "매월 21일 — 휴일이면 다음 평일일 수 있음")
        fri = nth_weekday(y, mo, 4, 1)
        kd, kt = et_to_kst(fri, 8, 30)
        add(kd, kt, "US", "미국 고용보고서(비농업 고용·실업률)", 5,
            "대개 첫째 금요일 08:30 ET — BLS 일정으로 확인 필요")
        if mo in (1, 4, 7, 10):
            add(m + timedelta(days=4), None, "KR", "삼성전자·LG전자 분기 잠정실적 시즌", 5,
                "분기 끝난 뒤 첫 주(대개 5~8일) — 보도로 날짜 확인")

    d = d0
    while d <= d5:
        if d.weekday() == 3:
            kd, kt = et_to_kst(d, 8, 30)
            add(kd, kt, "US", "미국 주간 신규 실업수당 청구", 3, "매주 목요일 08:30 ET")
        d += timedelta(days=1)

    out.sort(key=lambda e: (e["date"], e["time"] or "99:99"))
    return out


if __name__ == "__main__":
    d0 = (datetime.strptime(sys.argv[1], "%Y-%m-%d").date() if len(sys.argv) > 1
          else datetime.now(KST).date())
    d5 = d0 + timedelta(days=5)
    ev = rules(d0, d5)
    print(json.dumps({"window": [d0.isoformat(), d5.isoformat()], "candidates": ev},
                     ensure_ascii=False, indent=1))
    for e in ev:
        dd = date.fromisoformat(e["date"])
        print("# %s(%s) %s [%s] %s — %s" % (e["date"][5:], DOW[dd.weekday()], e["time"] or "-",
                                          e["region"], e["title"], e["rule"]), file=sys.stderr)
