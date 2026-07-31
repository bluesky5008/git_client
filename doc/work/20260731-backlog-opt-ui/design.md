# 설계 — backlog §4 + §5 증분

> 기준선: v1 (2026-07-31 승인) · 요구사항: [requirements.md](requirements.md)

## P1 — 설정형 최적화와 판정

**협상 알고리즘 (FR-01).** `BASE_CONFIG`에 `fetch.negotiationAlgorithm=skipping`
추가 — 단 채택 전에 실측한다: 서로 모르는 커밋이 많이 벌어진 픽스처에서
`negotiation_rounds`(복구된 계측)를 기본값과 비교. skipping은 공통 조상
탐색을 지수적으로 건너뛰어 왕복을 줄이는 대신 공통점을 덜 정밀하게 잡아
전송이 소폭 늘 수 있다 — 목적 함수상 느린 회선에서 RTT×왕복이 지배하므로
왕복 감소가 확인되면 채택. 감소가 없으면 채택하지 않고 기록만 남긴다.

**SSH 멀티플렉싱 (FR-02).** `_run`의 `GIT_SSH_COMMAND` setdefault를
플랫폼 분기: POSIX에서 `ssh -o BatchMode=yes -o ControlMaster=auto
-o ControlPath=<XDG_RUNTIME 또는 tmp>/gitclient-ssh-%C -o ControlPersist=60`.
Windows OpenSSH는 ControlMaster 미지원 — 기존 값 유지. 사용자가
GIT_SSH_COMMAND를 이미 설정했으면 존중(현행 setdefault 유지). 로컬에 ssh
원격이 없어 실측 불가 → 🔶 잠정 채택, 연결 재사용은 손해 경로가 없다는
논리 근거를 ADR에 기록.

**fsmonitor/untrackedCache (FR-03) — 기각.** 이 설정들의 소비자는
`git status`인데 앱의 상태 조회는 전부 pygit2(libgit2) 경로다 — libgit2는
fsmonitor 데몬도 untracked cache도 읽지 않는다. 켜면 데몬 프로세스만 남고
아무도 빨라지지 않는다. §7로 이동, 재평가 조건: 앱이 git CLI status를
쓰게 되거나 파일시스템 감시(§2.3)를 설계할 때.

**commit-graph (FR-04).** ADR-14 유지 — 소비 기능(경로별 히스토리) 등장
시 벤치 후 결정. 변경 없음.

## P2 — 중복 fetch 억제: 취소 대신 승격

현재: 사용자 fetch 시작 → `_cancel_prefetch()` → 받던 팩 폐기 → 같은
데이터를 0바이트부터 다시. 느린 회선에서 prefetch가 90% 왔어도 버린다.

설계: **같은 원격이면 승격(promote).**

```
사용자 fetch 요청
  ├─ prefetch 없음 → 기존 경로
  ├─ 다른 원격의 prefetch → 취소(양보), 기존 경로 (FR-06)
  └─ 같은 원격의 prefetch →
       1. 조용하던 prefetch의 progressed를 상태바에 연결 (표시 승격)
       2. retired 시점에 실제 FetchWorker 시작 — 객체가 이미 로컬이라
          이 fetch는 대개 전송 0 (README 실측)
       3. 취소 버튼은 승격된 prefetch를 죽인다 (FR-07)
```

근거: 죽여야 할 것은 "사용자를 기다리게 하는 배경 작업"이지 "사용자가
어차피 기다릴 같은 전송"이 아니다. 이미 실린 바이트를 버리고 재전송하는
것은 ADR-49(벽시계 타임아웃이 실어 나른 바이트를 버린다)가 막은 것과
같은 낭비다. 계측은 두 작업이 각각 기록된다(귀속 왜곡 없음).

구현 위치: `main_window._start_fetch`(및 pull 진입점)에서 분기.
`_promoted_prefetch` 상태 하나 추가, `_update_remote_actions`는 승격 중을
fetch 진행과 동일 취급.

## P3 — 유휴 repack

- 트리거: `_prefetch_timer`와 동일한 주기 축에서, 마지막 원격 작업·쓰기
  이후 N분(기본 10분) 경과 + 큐·워커·연산 전부 유휴일 때 저장소당 1회.
- 실행: WriteQueue 경유(§3.3 규칙 3 — 저장소 쓰기다),
  `git repack -a -d -q` + `pack.window`/`pack.depth` 상향(ADR-35의 값을
  쓸 유일한 자리 — 사용자가 기다리지 않는 CPU는 공짜다).
- 양보: 사용자 작업 제출 시 프로세스 트리 종료(기존 취소 인프라 재사용).
  git repack은 임시 팩에 쓰고 원자적으로 교체하므로 중단해도 안전하다.
- 검증: 팩 수 감소 + 양보 동작 + repack 후 팩 크기(델타 효과) 기록.

## U1 — reflog 탐색

- 엔진: `LocalGitEngine.head_reflog(limit=200)` → pygit2
  `repo.references["HEAD"].log()`에서 (sha, 요약, 시각, 메시지) 튜플.
  읽기 전용·상한 있음 — 다이얼로그를 여는 시점 1회라 UI 스레드 허용
  (200건 파싱은 ms 단위, G4 안).
- UI: 저장소 메뉴 "reflog 탐색...". 목록(시각·동작·sha·메시지) +
  "이 커밋에서 브랜치 만들기"(이름 입력 → 기존 브랜치 생성 잡을
  WriteQueue로 제출) + sha 복사.
- ADR-76·reset의 안내문("git reflog로 되찾을 수 있습니다")이 이제 앱 안
  경로를 가리키도록 문구 갱신.

## U2 — git 명령 로그 패널

- domain에 `CommandRecord(started, argv, cwd, duration_ms, returncode)` +
  프로세스 전역 링버퍼(상한 500) `CommandLog` — 구독 콜백 1개(UI).
- 기록 지점 둘: `RemoteEngine._run`(Popen 완료 시), `LocalGitEngine._run_git`.
  워커 스레드에서 쓰므로 램버퍼는 락으로 보호, UI 전달은 Qt 시그널 큐잉.
- 표시: 보기 메뉴 토글 도크 패널, 고정폭 글꼴, `실패(rc≠0)` 행 강조.
  URL userinfo는 `_without_userinfo`로 가린다 (FR-12). 자격증명은 ADR-78
  이후 명령줄에 원래 없다 — helper 문자열의 env 변수명만 노출되며 안전.
- pygit2 경유 쓰기는 git 명령이 아니므로 다루지 않는다 — 패널 이름이
  "실행된 git 명령"인 이유.

## U3~U5 — 이후 회차 (개요 설계)

- **U3 검색·전환**: 그래프 상단 검색줄(메시지·sha·작성자 부분일치,
  Ctrl+F, 다음/이전 점프 — 필터가 아니라 점프: 그래프 레인은 전체 맥락이
  있어야 읽힌다). 툴바에 최근 저장소 드롭다운(QSettings 최근 10개).
- **U4 설정·테마**: 설정 다이얼로그(프리페치, repack 주기, 테마 선택).
  테마는 palette 기반 라이트/다크 2종 + OS 추종(§5.3) — delegate 색상을
  theme.py 단일 출처로 이미 모아둔 구조를 활용.
- **U5**: 단축키(QKeySequence 설정화), 다국어(Qt tr() 래핑 + ko/en),
  DnD(참조 패널→그래프 브랜치 드롭 시 merge/rebase 메뉴).

## 위험

- P2: 승격 상태 기계가 취소·저장소 전환과 겹치는 경계 — 기존 정체 가드
  패턴(워커 identity 비교) 재사용으로 통제.
- P3: repack 중 앱 종료 — git이 임시 팩을 쓰다 죽어도 저장소는 일관
  (원자 교체 전). 잔여 tmp 팩은 다음 repack이 치운다.
- U2: 명령 기록이 성능에 주는 부담 — 기록은 명령당 1회 O(1) append.

## 검증 전략

각 증분의 AC(requirements.md)를 통합 테스트로. P1 협상은 실측 스크립트를
experiments/에 보존. 전체 스위트 회귀 0 유지.
