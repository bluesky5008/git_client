# 작업 기록 — backlog §4·§5 증분

## 기준선

- 요구사항·설계: 이 디렉터리 v1 (2026-07-31 사용자 승인 — P1·P2·U1·U2
  이번 회차, P3·U3~U5 설계만)
- 관련 ADR: ADR-80(협상 skipping)·81(SSH mux 🔶)·82(fsmonitor 기각)·83(승격)

## 계획 진행 상태

- [x] P1 — 협상 실측→채택, SSH 멀티플렉싱(POSIX), fsmonitor 판정, commit-graph 보류 확인
- [x] P2 — 같은 원격 prefetch 승격 (취소 버튼·양보 경계 포함)
- [x] U1 — reflog 탐색 + "이 커밋에서 브랜치 만들기"
- [x] U2 — 실행된 git 명령 로그 패널 (userinfo 마스킹)
- [x] P3 — 유휴 repack (2026-07-31 구현)
- [ ] U3~U5 — 검색·전환 / 설정·테마 / 단축키·다국어·DnD (설계 승인, 다음 회차)

## 변경 요약

| 파일 | 변경 |
|------|------|
| infrastructure/remote_engine.py | BASE_CONFIG에 `fetch.negotiationAlgorithm=skipping`, `_ssh_command()` 플랫폼 분기, 명령 로그 기록 |
| infrastructure/local_engine.py | `head_reflog()`, `create_branch(sha=)`, 시퀀서 명령 로그 기록 |
| domain/models.py | `ReflogEntry` |
| domain/command_log.py | 신규 — `CommandRecord`·`CommandLog` 링버퍼(500), userinfo 마스킹 |
| application/remote_workers.py | `RemoteWorker.is_cancelled` |
| ui/main_window.py | prefetch 승격(`_promote_prefetch`/`_on_promoted_retired`), reflog 메뉴·핸들러, 보기 메뉴 + 로그 도크 |
| ui/reflog_dialog.py · ui/command_log_panel.py | 신규 |

## 검증 (2026-07-31, macOS · git 2.50.1)

| AC | 검증 | 결과 |
|---|---|---|
| AC-01 협상 왕복 감소 | 실측 5→1 (experiments/exp_negotiation.py) + test_optimizations | 성공 — 채택 |
| AC-02 SSH 분기 | test_optimizations (POSIX 포함·Windows 제외·소켓 경로 상한) | 성공 |
| AC-03 승격 | test_prefetch (승격·취소·push 양보 3건) — 기존 취소 테스트는 승인된 계약 변경에 맞춰 교체 | 성공 |
| AC-05 reflog 복구 | test_reflog 7건 (잃기→찾기→브랜치 복구 전 구간 + 창 경유) | 성공 |
| AC-06 명령 로그 | test_command_log 6건 (마스킹·링버퍼·실제 fetch·시퀀서·패널) | 성공 |
| AC-04 유휴 repack | test_idle_repack 6건 — 팩 3개→1개, 양보(시작 전·중 판정), bare 제외, 유휴 판정·시계 되감기 | 성공 |

**P3 델타 효과 실측** (이 저장소 자신, `--no-local` 클론): 기본 repack
776KB/34ms vs `window/depth=250` 776KB/32ms — **이 규모에서는 0%**다.
ADR-35의 "이득은 저장소가 커질수록 준다"와 일치한다. 유휴 repack의 실제
가치는 `unpackLimit=1`이 쌓는 팩의 통합이고, 델타 설정은 유휴 CPU가
공짜라 잃을 것이 없어 얹은 실험이다 — 크기 절감을 근거로 삼지 않는다.

전체 스위트: 아래 최종 실행 참조 (커밋 메시지에 수치 기록).

## 남은 사항

- U3~U5 구현 (설계 승인 상태로 대기)
- ADR-81의 ssh 실측 (ssh 원격 확보 시), Windows 스위트 재실행
