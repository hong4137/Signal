# Signal Layer

개발자 커뮤니티 담론을 **규모**와 **논쟁도** 두 축으로 읽는 트렌드 계기판.

기존 뉴스 파이프라인(외신 브리핑 · Must News · 주간 브리핑)은 입력이 전부
**"매체가 이미 기사로 만든 것"**이다. 커뮤니티 담론은 그 이전에 벌어지고,
상당수는 끝내 기사가 안 된다. 이 저장소가 그 층을 맡는다.

→ https://hong4137.github.io/Signal/

## 핵심 원칙

> 유형을 알아맞히려 하지 말고, **"커뮤니티가 시끄러웠다"는 사실 자체**를 신호로 삼는다.
> 그러면 신기한 AI 행동 경험담이든 퇴사자 발언이든 분류하지 않아도 걸린다.

- **댓글 수** = 참여 규모 · **r = 댓글 ÷ 업보트** = 논쟁 성격
- 두 지표를 **한 점수로 뭉개지 않는다.** 각각 정렬해 병기한다
- 하루 산출 5~10건이면 성공, 30건이면 설계 실패

키워드로 `left Anthropic` 을 찾으면 **0건**이지만, buzz 정렬은
`I resigned from Anthropic today` (732pt / 1012댓글 / r 1.38) 를 즉시 잡는다.

## 층

| 층 | 역할 | 접근 |
|---|---|---|
| Hacker News | buzz 본체 | Algolia API, 무인증 |
| Reddit | "신기한 AI 행동" 본진 | `.rss` + **고유 UA 필수** (`.json` 은 403) |
| HuggingFace | 모델 트렌드 | 무인증 |
| 신뢰층 RSS | 해석·프레이밍 | Simon Willison · Latent Space · Zvi 등 |
| GitHub | 신규 프로젝트 | 스팸 필터 필수 |
| Threads 패널 | 국내 담론 | **Cloudflare Browser Rendering** (JS 렌더 필요) |

## 구성

```
collect.py            수집 — 순수 HTTP. 어디서든 돈다
enrich.py             한국어 요약(HN 댓글 기반) + 국내 대응 보도 매칭
scripts/fetch_panel.py  Threads 패널 — CF Worker 에 위임
cf-threads/           Cloudflare Worker (Browser Rendering)
index.html            뷰어 — data/*.json 을 읽는다
data/                 수집 결과 (Actions 가 커밋)
```

## 실행

```bash
python collect.py --days 3 --min-comments 120   # 일간 권장값
python enrich.py  --limit 12                    # GEMINI_API_KEY 필요
python enrich.py  --rematch                     # LLM 없이 국내 매칭만 다시
```

## Actions Secrets

| 이름 | 용도 | 없으면 |
|---|---|---|
| `GEMINI_API_KEY` | 한국어 요약 | 보강 건너뜀 (수집은 정상) |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | 국내 보도 매칭 | 국내 칸 비움 |
| `THREADS_WORKER_URL` | 배포한 Worker 주소 | 패널 건너뜀 |

**키를 파일에 넣지 말 것.** Secrets 로만 넣는다.

## 다시 밟지 말 것

- `story_key` 에서 **쿼리스트링을 버리면 안 된다.** HN 스레드가 전부
  `url:news.ycombinator.com/item` 하나로 뭉개져 매칭률 100% 가 된다(실측 사고)
- 묶음 뉴스레터(Latent Space `[AINews]`)는 링크 매칭에서 제외한다. 안 그러면 전 항목에 붙는다
- Reddit 에 `Mozilla/5.0` 같은 일반 UA 를 쓰면 `.rss` 도 403 이다
- Threads 를 정적 fetch 하면 **278KB 짜리 빈 껍데기**가 온다. 본문은 JS 가 그린다
- 국내 매칭에 키워드 하나만 겹쳐도 붙이면 오탐이 난다
  (`military` 하나로 "미군 AI 환각" ↔ "병역특례 부활" 이 붙었다)

설계 근거 전문은 `RESUME_SIGNAL_LAYER_20260914.md` 참조.
