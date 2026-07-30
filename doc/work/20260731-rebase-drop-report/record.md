# 작업 기록 — DCR-001 구현

## 기준선

- 관련 설계: DCR-001 (승인 2026-07-31), design.md v2.4, ADR-76
- 관련 ADR: ADR-69·73 (일관), ADR-65 (잔재 종결)

## 변경 요약

| 파일 | 변경 |
|------|------|
| domain/models.py | `HistoryOutcomeKind.WOULD_BE_EMPTY`, `HistoryOutcome.skipped_already_applied`, `is_would_be_empty` |
| infrastructure/local_engine.py | rebase에 `--empty=stop`, `_sequencer`의 빈 멈춤을 오류 대신 `WOULD_BE_EMPTY`로, `continue_operation()` 직전 트리 비교, `keep_empty_operation()`, `_stalled_commit_summary()`, `skipped previously applied` 경고 파싱 |
| application/remote_workers.py | `keep_empty_operation_job()` |
| ui/main_window.py | `_enter_would_be_empty()` 선택 다이얼로그(버리기·빈 커밋 유지·중단·나중에), 완료 시 생략 커밋 알림 |

## 검증 (2026-07-31, macOS · git 2.50.1 · Python 3.12.13)

| 인수 조건 | 검증 | 결과 |
|---|---|---|
| 충돌→upstream 해결→계속 시 소실 0 | test_continue_stops_before_git_would_drop_the_commit | 성공 |
| 건너뛰기 → 그 커밋만 빠짐 | test_skip_after_the_stop_drops_exactly_that_commit | 성공 |
| 유지 → 메시지 보존한 빈 커밋 | test_keep_empty_preserves_the_commit_message (+ cherry-pick 변형) | 성공 |
| 부분집합 커밋 → 멈춰서 물음 | test_rebase_stops_on_a_commit_that_becomes_empty | 성공 |
| 정당한 생략 → 완료 + 보고, 거짓 경고 없음 | test_already_applied_commits_are_reported_not_hidden | 성공 |
| 평범한 리베이스는 멈추지 않음 | test_an_ordinary_rebase_does_not_stop | 성공 |

전체 스위트 726 통과 / 24 실패(전부 사전 분류된 최신 git 비호환 — backlog
§3.10~3.12, 이 변경과 무관) / 4 skip. 기존 테스트 1건은 승인된 동작 변경에
맞춰 갱신(test_continue_reports_an_empty_result_as_a_choice — 오류 → 선택).

## 남은 사항

- cherry-pick·revert의 자체 거부 메시지를 `WOULD_BE_EMPTY`로 번역하는 것은
  불필요해졌다 — continue 직전 판정이 세 연산에 공통으로 걸린다(테스트 확인).
- Windows에서의 스위트 재실행은 미수행 (이 환경에 없음).
