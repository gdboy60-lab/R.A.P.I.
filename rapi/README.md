# R.A.P.I

초보 투자자의 가상 매매 습관을 14개 규칙으로 평가하고 AI 피드백을 주는 모바일 웹 서비스 (수업·공모전 시연용 MVP).
설계 기준은 규칙 명세서와 PRD를 따른다.

## 처음 실행하기

깃허브에서 받은 직후에도 아래 한 줄이면 된다. 가상환경 만들기, 패키지 설치, 서버 실행을 차례로 하고,
`rapi.db` 가 없으면 서버가 처음 켜질 때 자동으로 만들어 개발용 예시 기간을 넣는다.

```bash
.\run.ps1          # Windows PowerShell
bash run.sh        # Mac / GitHub Codespaces
```

브라우저에서 http://127.0.0.1:8000 을 연다. 직접 명령으로 하려면:

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1                  # Mac: source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --reload
python -m pytest -q                         # 테스트
```

Windows에서 `pip.exe`·`uvicorn.exe` 가 "애플리케이션 제어 정책"으로 막히면, 명령 앞에 항상 `python -m` 을 붙인다.
`.\run.ps1` 이 "스크립트를 실행할 수 없습니다"로 막히면 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` 를 한 번 실행한다.

지금 동작하는 흐름: 로그인(체험으로 시작) → 프로젝트 시작 → 홈 · 시장(관심 종목) · 종목 상세(매수·매도, 매매 계획) · 거래 기록(미체결 취소, 손익). 상단 시계 바로 장 열기·일시정지·속도·장 마감까지·다음 거래일.

이전 버전에서 만든 `rapi.db` 에는 가상 일봉이 없다. 새 버전으로 바꾼 뒤에는 `rapi.db` 를 지우고 서버를 다시 켠다.

## 지금 되는 것

로그인(체험으로 시작) → 프로젝트 시작 → 홈 · 시장(관심 종목) · 종목 상세(매수·매도, 매매 계획) · 거래 기록(미체결 취소, 손익).
상단 시계 바로 장 열기·일시정지·속도·장 마감까지. 장 마감에 닿으면 결과 화면으로 넘어가 그날 감점을 한꺼번에 공개하고, 그 화면에서만 다음 거래일로 갈 수 있다. 마지막 날(또는 조기 종료)에는 최종 점수가 확정되고 **최종 리포트**(`/report`)로 넘어간다: 최종 점수, 일차별 감점 막대, 자주 나온 위반 상위 3개, 처음 5일과 이후 비교, 제안 행동 실행률, 다음에 집중할 습관, 판정하지 못한 기록. 숫자는 바로 보이고 AI 설명은 이어서 채워진다 (한 번 만든 리포트는 저장돼 다시 열어도 같다).

이전 버전에서 만든 `rapi.db` 에는 가상 일봉이 없다. 새 버전으로 바꾼 뒤에는 서버를 끄고 `rapi.db` 를 지운 다음 다시 켠다.

## AI 피드백 켜기

장 마감 결과 화면이 열리면 그날 피드백을 만든다. API 키가 없거나 호출이 실패하면 규칙 기반 기본 피드백으로 채운다.

1. https://platform.claude.com 에서 API 키를 만든다 (Claude 채팅 구독과 별도 결제)
2. 깃허브 저장소 **Settings → Secrets and variables → Codespaces → New repository secret**
   이름 `ANTHROPIC_API_KEY`, 값에 키를 넣는다. 코드나 채팅에 키를 붙여넣지 않는다.
3. Codespace를 다시 시작하면 적용된다. 모델은 `app/settings.py` 의 `AI_MODEL` (기본 `claude-haiku-5-5`)

결과 화면의 피드백 칸 오른쪽 위 문구로 상태를 알 수 있다: "판정 기록에 근거해 작성됐어요"(AI), "기본 피드백 (AI 키 없음)", "AI 연결이 안 돼서 기본 피드백을 보여줘요"(실패).

## 실제 일봉 데이터 넣기 (B)

1. 공공데이터포털에서 **금융위원회_주식시세정보** 활용신청 → 마이페이지에서 일반 인증키(Decoding) 복사
2. `periods.example.json` 을 복사해 `periods.json` 을 만들고 기간·종목을 고친다 (개장일 10일 이상)
3. 실행 (PowerShell)

```bash
$env:DATA_GO_KR_KEY="발급받은_인증키"
python -m app.data.load_prices periods.json
```

기간마다 시작 약 400일 전부터 일봉을 받아 `daily_prices`, `market_calendar`, `replay_periods`, `replay_stocks` 를 채운다. 기간 앞 일봉이 250일보다 적은 종목은 "주의"로 알려준다.

지킬 것: 공식 API로만 받기(크롤링 금지), `rapi.db`·`periods.json`·CSV를 깃허브에 올리지 않기(재배포 금지, `.gitignore` 에 등록됨), 화면에 출처 표시.

## 폴더 구조

| 경로 | 내용 | 1주차 담당 |
| --- | --- | --- |
| `schema.sql` | 20개 테이블과 제약. 테이블 정의의 원본 | A |
| `app/db.py` | 연결, 초기화 | A |
| `app/engine/clock.py` | 시뮬레이션 시계 (DB와 무관한 순수 계산) | A |
| `app/engine/clock_store.py` | 시계를 projects 테이블에 읽고 쓰기 | A |
| `app/engine/prices.py` | 시세 공급(리플레이): 일봉에 맞춘 장중 가격·누적 거래량 | A |
| `app/engine/trading.py` | 체결 엔진: 주문, 체결, 수수료·세금, 거래 묶기, 손절·목표가 도달, 장 마감 취소 | A |
| `app/engine/indicators.py` | 20일 이동평균·평균 거래량, 와일더 RSI | B |
| `app/rules/engine.py` | 14개 규칙 판정 (매수 체결·거래 종료·장 마감 때), 중복 감점 방지, 최종 점수 확정 | B |
| `app/rules/describe.py` | 판정 기록을 화면 문장으로 | B |
| `app/day_close.py` | 장 마감 처리 순서 (취소 → 판정 → 공개 → 최종 확정) | A |
| `app/ai/feedback.py` | AI 일일 피드백: 입력 계산, Claude 호출, 근거 번호 검증, 재시도, 규칙 기반 대체 | C |
| `app/ai/report.py` | 최종 리포트: 집계(백엔드)와 AI 설명(근거 번호·숫자 검증, 규칙 기반 대체) | C |
| `app/settings.py` | 수수료·세율 등 설정값 (임시값, 시연 전 확인) | 모두 |
| `app/projects.py` | 체험 계정, 프로젝트·계좌 만들기 | A |
| `app/rules/catalog.py` | 14개 규칙 목록(영역·감점·판정 시점), 점수 계산 | B |
| `app/seed_dev.py` | 개발용 예시 데이터 (실제 데이터가 오면 대체) | B |
| `app/main.py` | FastAPI 라우트 | C |
| `app/templates/`, `app/static/` | 화면(Jinja2), 시계 바 JS, 스타일 | C |
| `tests/` | 스키마 제약, 시계, 점수 테스트 | 모두 |

## 데이터베이스 제약 (NFR-03)

| 규칙 | 구현 |
| --- | --- |
| 사용자당 진행 중 프로젝트 1개 | `uq_one_live_project` 부분 고유 인덱스 |
| 같은 규칙 + 같은 대상 중복 감점 금지 | `uq_rule_target` 고유 인덱스 (NULL 은 COALESCE 로 처리) |
| 관심 종목 동시 20개 | `trg_watchlist_max20` 트리거 |
| 종목당 진행 중 거래 1개 | `uq_one_open_trade` 부분 고유 인덱스 |
| 위반이면 감점 > 0, 보류·무효면 0 | `rule_evaluations` CHECK |

## 시계가 동작하는 방식

- 화면이 장중에 1초마다 `GET /api/clock` 을 부르면, 서버가 "실제 경과 시간 × 배속"만큼 시계를 진행한다.
- 한 번에 인정하는 실제 시간은 최대 5초다. 그래서 앱을 닫아 호출이 끊기면 시계가 멈춘다.
- 시계는 시뮬레이션 1초마다 `on_second(t)` 콜백을 부른다. 체결 엔진의 `TickWatcher` 가 이 콜백으로 지정가 체결과 손절·목표가 도달을 확인한다.
- 장 마감까지 건너뛰기도 1초씩 모두 처리한다 (하루 23,400번 콜백).

## 다음에 붙일 곳

| 주차 | 위치 |
| --- | --- |
| 2주 | (완료) 체결 엔진, 시장·주문·거래 기록 화면, 14개 규칙 판정, 장 마감 결과 화면 |
| 3주 | (완료) AI 일일 피드백, 피드백 탭 |
| 4주 | (완료) 최종 리포트 집계와 화면 / 남은 일: 실제 데이터 넣기, 시연 전 `debug=False`, 수수료·세율 확인 |

## 참고

- 체험 계정은 브라우저 쿠키로 이어보기를 하는 시연용 단순 방식이다. 공개 서비스라면 서명된 세션으로 바꿔야 한다.
- 13-1에서 정한 SQLAlchemy는 아직 쓰지 않았다. 지금은 표준 라이브러리 `sqlite3` 로 `schema.sql` 을 그대로 쓰며, 필요해지면 같은 DB 파일에 SQLAlchemy를 연결하면 된다.
