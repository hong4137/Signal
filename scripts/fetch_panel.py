#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Threads 패널 수집 — Cloudflare Worker 에 위임한다.

왜 Worker 인가
  threads.com 은 본문을 JS 로 그린다. 정적 요청은 빈 껍데기(278KB, 한글 0건).
  Actions 러너에 브라우저를 깔 수도 있지만, Worker 쪽이
    · 무료 한도 안(하루 10분 중 24초 사용)
    · 러너에 의존성 0
    · 메타가 Cloudflare IP 를 막지 않음(2026-09-23 실측 5/5 성공)
  이라 낫다.

환경변수
  THREADS_WORKER_URL   배포한 Worker 주소 (예: https://signal-threads.<계정>.workers.dev)
"""
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "panel_kr.json")

# 티어는 팔로워가 아니라 **내용**으로 매긴다 (2026-09-23 교정)
PANEL = [
    ("choi.openai",  "CHOI",        "A", "최신 AI 릴리스 한국어 속보"),
    ("jojoldu",      "이동욱 (향로)", "A", "개발 현장 도구 관찰"),
    ("integer.han",  "한정수",       "B", "직접 써본 실측 경험"),
    ("hiconcep",     "정지훈",       "B", "AI 에이전트 운영 경험"),
    ("wooviewing",   "우뷰",         "C", "반도체·인프라 투자 관점"),
]
MAX_POSTS = 3
MIN_LEN = 40          # 이보다 짧은 줄은 UI 부스러기다


def parse_posts(handle, text):
    """Worker 가 준 innerText 에서 글만 추려낸다.
    프로필 머리말·탭 이름·푸터를 걷어내고 문장만 남긴다."""
    skip = re.compile(
        r"^(로그인|팔로우|팔로워|언급|스레드|답글|미디어|리포스트|고정됨|"
        r"Follow|Mention|Threads|Replies|Media|Reposts|Pinned|"
        r"Log in|© \d{4}|Threads 약관|개인정보처리방침|쿠키|문제 신고|"
        r"Instagram으로|\d+ followers)")
    out, seen = [], set()
    for ln in (text or "").split("\n"):
        ln = ln.strip()
        if len(ln) < MIN_LEN or skip.match(ln) or ln == handle:
            continue
        if ln in seen:
            continue
        seen.add(ln)
        out.append(ln)
        if len(out) >= MAX_POSTS:
            break
    return out


def main():
    worker = (os.environ.get("THREADS_WORKER_URL") or "").rstrip("/")
    if not worker:
        print("THREADS_WORKER_URL 미설정 — 패널 수집 건너뜀", file=sys.stderr)
        return

    key = os.environ.get("THREADS_WORKER_KEY", "")
    if not key:
        print("THREADS_WORKER_KEY 미설정 — Worker 가 401 을 돌려줄 것이다", file=sys.stderr)

    handles = ",".join(h for h, *_ in PANEL)
    url = "%s/?h=%s&debug=1" % (worker, urllib.parse.quote(handles))
    try:
        # 키는 쿼리스트링이 아니라 헤더로 보낸다 — URL 은 로그에 남는다
        req = urllib.request.Request(url, headers={
            "User-Agent": "signal-actions/1.0",
            "Authorization": "Bearer " + key,
        })
        with urllib.request.urlopen(req, timeout=180) as r:
            d = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print("Worker %s: %s" % (e.code, e.read()[:160].decode("utf-8", "replace")),
              file=sys.stderr)
        sys.exit(0)
    except Exception as e:
        print("Worker 호출 실패: %s" % e, file=sys.stderr)
        sys.exit(0)          # 패널 실패가 전체 워크플로를 죽이지 않게

    if not d.get("ok"):
        print("Worker 오류: %s" % json.dumps(d, ensure_ascii=False)[:200], file=sys.stderr)
        sys.exit(0)

    meta = {h: (n, t, r) for h, n, t, r in PANEL}
    accounts, blocked = [], []
    for res in d.get("results", []):
        h = res.get("h")
        if res.get("walled"):
            blocked.append(h)
            continue
        posts = parse_posts(h, res.get("text", ""))
        if not posts:
            continue
        n, tier, role = meta.get(h, (h, "C", ""))
        accounts.append({
            "h": h, "n": n, "tier": tier, "role": role,
            "bio": "", "followers": "",
            "posts": [{"at": "", "t": p, "react": 0, "link": ""} for p in posts],
        })

    payload = {
        "_note": "국내 Threads 패널. Cloudflare Browser Rendering 경유 수집.",
        "collected": time.strftime("%Y-%m-%d %H:%M", time.gmtime(time.time() + 9 * 3600)),
        "accounts": accounts,
        "blocked": blocked,
        "demoted": [],
    }
    # 수동 큐레이션분(bio·tier 설명 등)이 있으면 보존
    old = None
    try:
        old = json.load(io.open(OUT, encoding="utf-8"))
    except Exception:
        pass
    if old:
        prev = {a["h"]: a for a in old.get("accounts", [])}
        for a in payload["accounts"]:
            p = prev.get(a["h"])
            if p:
                a["bio"] = a["bio"] or p.get("bio", "")
                a["followers"] = a["followers"] or p.get("followers", "")
        payload["demoted"] = old.get("demoted", [])

    io.open(OUT, "w", encoding="utf-8").write(
        json.dumps(payload, ensure_ascii=False, indent=1))
    print("패널 %d계정 · 글 %d개%s" % (
        len(accounts), sum(len(a["posts"]) for a in accounts),
        (" · 차단 %s" % ",".join(blocked)) if blocked else ""))


if __name__ == "__main__":
    main()
