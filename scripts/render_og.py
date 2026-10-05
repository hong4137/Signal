#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
링크 미리보기 — 그날 1면으로 썸네일(data/og.png)을 찍고 front.html 의 og 태그를 갈아끼운다.

왜 매번 찍나
  카톡·슬랙은 og:image 한 장으로 링크를 소개한다. 고정 로고보다 '오늘의 톱'이
  보이는 편이 열어볼 이유가 된다. build_front.py 다음에 돈다.

왜 front.html 을 고치나
  카톡 스크래퍼는 JS 를 실행하지 않는다. og 태그는 HTML 에 박혀 있어야 한다.
  <!--og:start--> … <!--og:end--> 사이만 다시 쓴다.
  이미지 주소에 ?v=<조판시각> 을 붙여 캐시된 옛 썸네일이 남지 않게 한다.

크롬
  CHROME 환경변수 → google-chrome / chromium / msedge 순으로 찾는다.
  Actions(ubuntu-latest)에는 google-chrome 이 깔려 있다. 못 찍으면 태그도 안 바꾼다.
"""
import html
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
SITE = "https://signal.sharktalk.co.kr"
DOW = "월화수목금토일"


def chrome():
    cands = [os.environ.get("CHROME"), shutil.which("google-chrome"), shutil.which("google-chrome-stable"),
             shutil.which("chromium"), shutil.which("chromium-browser"),
             r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
             r"C:\Program Files\Google\Chrome\Application\chrome.exe"]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


def main():
    F = json.load(io.open(os.path.join(DATA, "front.json"), encoding="utf-8"))
    L = F.get("lead") or {}
    R = L.get("reaction") or {}
    d = datetime.strptime(F["edition"][:10], "%Y-%m-%d")
    date_ko = "%d년 %d월 %d일 %s요일" % (d.year, d.month, d.day, DOW[d.weekday()])

    hist = {}
    try:
        hist = json.load(io.open(os.path.join(DATA, "trend_history.json"), encoding="utf-8"))
    except Exception:
        pass
    days = sorted((hist.get("days") or {}).keys())
    wave = [sum(v for k, v in hist["days"][x].items() if k != "n" and isinstance(v, (int, float)))
            for x in days]

    stats = []
    if R:
        stats.append({"n": "{:,}".format(R.get("comments", 0)), "l": "커뮤니티 댓글", "c": "#D2603A"})
        stats.append({"n": "%.2f" % R.get("r", 0), "l": "추천당 댓글", "c": "#F4F2EC"})
    n_for = sum(len(s["items"]) for s in F.get("foreign") or []) + len(F.get("left") or []) + \
        (1 if L.get("kind") == "cross" else 0)
    if n_for:
        stats.append({"n": str(n_for), "l": "외신 기사", "c": "#18A2C6"})

    img = (L.get("img") or {}).get("src")
    data = {
        "date": date_ko, "no": F.get("no"),
        "kicker": "오늘의 1면 · 외신 × 커뮤니티" if L.get("kind") == "cross" else "오늘의 1면 · 커뮤니티 최대 토론",
        "headline": L.get("headline") or "오늘의 AI·테크 1면",
        "img": img, "stats": stats, "wave": wave[-61:],
    }
    tpl = io.open(os.path.join(ROOT, "scripts", "og_card.html"), encoding="utf-8").read()
    page = tpl.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))

    exe = chrome()
    if not exe:
        print("  [og] 크롬 없음 — 썸네일 건너뜀", file=sys.stderr)
        return
    out = os.path.join(DATA, "og.png")
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "og.html")
        io.open(src, "w", encoding="utf-8").write(page)
        tmp = os.path.join(td, "og.png")
        cmd = [exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-sandbox",
               "--force-device-scale-factor=1", "--window-size=1200,630",
               "--virtual-time-budget=15000", "--screenshot=" + tmp, pathlib.Path(src).as_uri()]
        try:
            subprocess.run(cmd, timeout=90, capture_output=True)
        except Exception as e:
            print("  [og] 크롬 실패: %s" % e, file=sys.stderr)
            return
        if not os.path.exists(tmp) or os.path.getsize(tmp) < 20000:
            print("  [og] 스크린샷이 비었다 — 기존 썸네일 유지", file=sys.stderr)
            return
        shutil.copyfile(tmp, out)

    # ── front.html 의 og 태그 ──
    v = re.sub(r"\D", "", F["edition"])
    desc = "오늘의 톱 — %s" % data["headline"]
    if R:
        desc += " (커뮤니티 댓글 {:,})".format(R.get("comments", 0))
    desc += " · 외신·국내·커뮤니티 반향을 한 면에."
    e = lambda s: html.escape(s, quote=True)
    block = "\n".join([
        "<!--og:start-->",
        '<meta name="description" content="%s">' % e(desc),
        '<meta property="og:type" content="website">',
        '<meta property="og:site_name" content="Daily UptoDate">',
        '<meta property="og:title" content="%s">' % e("Daily UptoDate · " + date_ko),
        '<meta property="og:description" content="%s">' % e(desc),
        '<meta property="og:url" content="%s/front.html">' % SITE,
        '<meta property="og:image" content="%s/data/og.png?v=%s">' % (SITE, v),
        '<meta property="og:image:width" content="1200">',
        '<meta property="og:image:height" content="630">',
        '<meta property="og:image:alt" content="%s">' % e("Daily UptoDate %s 1면 — %s" % (date_ko, data["headline"])),
        '<meta name="twitter:card" content="summary_large_image">',
        "<!--og:end-->",
    ])
    p = os.path.join(ROOT, "front.html")
    s = io.open(p, encoding="utf-8").read()
    s2 = re.sub(r"<!--og:start-->.*?<!--og:end-->", lambda m: block, s, flags=re.S)
    if s2 != s:
        io.open(p, "w", encoding="utf-8", newline="\n").write(s2)
    print("  og 썸네일 · %s · %s" % (date_ko, data["headline"][:30]))


if __name__ == "__main__":
    main()
