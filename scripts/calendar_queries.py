"""경제 일정 루틴의 검색 계획 — data/calendar_playbook.json 을 읽고 쓴다.

  python3 scripts/calendar_queries.py plan [YYYY-MM-DD]
      오늘(KST) 실행할 검색어 목록(빈칸 채움, 시기 맞는 것만) + 규칙 일정 후보를 JSON 으로.
  python3 scripts/calendar_queries.py record <결과.json>
      검색어별 성과를 계획표에 적는다. 결과.json 형식:
      {"hits": {"<검색어 id>": 건진 일정 수, ...},          ← 실행한 검색어는 0 이라도 모두 적는다
       "proposed": [{"q": "...", "when": "always", "why": "..."}],   ← 효과 본 새 검색어(최대 3)
       "learned": ["다음에 참고할 한 줄"]}
      trial 검색어가 7회 연속 0건이면 retired 로 내린다. core 는 사람이 관리한다.
  python3 scripts/calendar_queries.py check
      계획표 형식 검사.
"""
import io
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from calendar_rules import rules  # noqa: E402

KST = timezone(timedelta(hours=9))
PB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data", "calendar_playbook.json")
EN_MONTH = ["January", "February", "March", "April", "May", "June", "July", "August",
            "September", "October", "November", "December"]
MAX_TRIAL = 8


def load():
    return json.load(io.open(PB, encoding="utf-8"))


def save(pb):
    io.open(PB, "w", encoding="utf-8", newline="\n").write(json.dumps(pb, ensure_ascii=False, indent=1) + "\n")


def variables(d0, pb):
    # 월~수는 이번 주 월요일, 목~일은 다음 주 월요일 (주간 일정표가 그때 나온다)
    mon = d0 - timedelta(days=d0.weekday()) + (timedelta(weeks=1) if d0.weekday() >= 3 else timedelta(0))
    q = (d0.month - 1) // 3 or 4            # 직전 분기
    qy = d0.year if d0.month > 3 else d0.year - 1
    d5 = d0 + timedelta(days=5)
    return {"y": d0.year, "m": d0.month, "wk": (d0.day - 1) // 7 + 1,
            "d0": "%d월 %d일" % (d0.month, d0.day), "d5": "%d월 %d일" % (d5.month, d5.day),
            "mon": "%d월 %d일" % (mon.month, mon.day), "mon_en": "%s %d" % (EN_MONTH[mon.month - 1], mon.day),
            "q": q, "qy": qy,
            "watch_kr": " ".join(pb["watch"]["kr"]), "watch_us": " ".join(pb["watch"]["us"])}


def applies(when, d0):
    for part in re.split(r",(?=[a-z]+:)", when or "always"):
        part = part.strip()
        if part in ("", "always"):
            continue
        k, _, v = part.partition(":")
        if k == "months":
            if d0.month not in {int(x) for x in v.split(",")}:
                return False
        elif k in ("days", "dow"):
            lo, _, hi = v.partition("-")
            x = d0.day if k == "days" else d0.weekday()
            if not int(lo) <= x <= int(hi or lo):
                return False
        else:
            raise ValueError("모르는 when 조건: " + part)
    return True


def fill(q, var):
    out = re.sub(r"\{(\w+)\}", lambda m: str(var[m.group(1)]), q)
    return out


def plan(d0):
    pb = load()
    var = variables(d0, pb)
    qs = []
    for x in pb["queries"]:
        if x.get("status") == "retired" or not applies(x.get("when"), d0):
            continue
        qs.append({"id": x["id"], "q": fill(x["q"], var), "why": x.get("why", ""),
                   "status": x.get("status", "core")})
    d5 = d0 + timedelta(days=5)
    return {"today": d0.isoformat(), "window": [d0.isoformat(), d5.isoformat()],
            "queries": qs, "rule_candidates": rules(d0, d5),
            "trusted": pb.get("trusted", {}), "learned": pb.get("learned", [])[-10:]}


def record(path, d0):
    pb = load()
    res = json.load(io.open(path, encoding="utf-8"))
    ids = {x["id"]: x for x in pb["queries"]}
    st = pb.setdefault("stats", {})
    for qid, n in (res.get("hits") or {}).items():
        if qid not in ids:
            continue
        s = st.setdefault(qid, {"runs": 0, "hits": 0, "zero_streak": 0})
        n = int(n or 0)
        s["runs"] += 1
        s["hits"] += n
        s["zero_streak"] = 0 if n else s["zero_streak"] + 1
        s["last_run"] = d0.isoformat()
        if n:
            s["last_hit"] = d0.isoformat()
        if ids[qid].get("status") == "trial" and s["zero_streak"] >= 7:
            ids[qid]["status"] = "retired"
    have = {x["q"].strip() for x in pb["queries"]}
    active_trial = sum(1 for x in pb["queries"] if x.get("status") == "trial")
    for i, p in enumerate((res.get("proposed") or [])[:3]):
        q = str(p.get("q", "")).strip()
        if not q or q in have or active_trial >= MAX_TRIAL:
            continue
        applies(p.get("when", "always"), d0)          # 형식 검사
        pb["queries"].append({"id": "t-%s-%d" % (d0.strftime("%Y%m%d"), i + 1), "q": q,
                              "when": p.get("when", "always"), "status": "trial",
                              "why": str(p.get("why", ""))[:80], "added": d0.isoformat()})
        have.add(q)
        active_trial += 1
    for note in (res.get("learned") or [])[:3]:
        pb.setdefault("learned", []).append({"date": d0.isoformat(), "note": str(note)[:120]})
    pb["learned"] = pb.get("learned", [])[-30:]
    check(pb)
    save(pb)
    return pb


def check(pb=None):
    pb = pb or load()
    seen = set()
    var = variables(date(2026, 1, 5), pb)
    for x in pb["queries"]:
        assert x["id"] not in seen, "id 중복: " + x["id"]
        seen.add(x["id"])
        assert x.get("status") in ("core", "trial", "retired"), x["id"]
        applies(x.get("when"), date(2026, 1, 5))
        fill(x["q"], var)                              # 모르는 변수면 KeyError
    return True


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    today = datetime.now(KST).date()
    if cmd == "plan":
        d0 = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else today
        print(json.dumps(plan(d0), ensure_ascii=False, indent=1))
    elif cmd == "record":
        pb = record(sys.argv[2], today)
        print("기록 완료 — 검색어 %d개 (trial %d, retired %d)" % (
            len(pb["queries"]), sum(x.get("status") == "trial" for x in pb["queries"]),
            sum(x.get("status") == "retired" for x in pb["queries"])))
    elif cmd == "check":
        check()
        print("ok")
    else:
        sys.exit(__doc__)
