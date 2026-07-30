# 작업 기록 — DCR-003 구현 (+ backlog §3.11)

## 기준선

- 관련 설계: DCR-003 (제안 — 구현·검증 완료, 사후 승인 대기), design.md v2.5, ADR-78
- 관련 ADR: ADR-28·29·32·33 유지, ADR-31 대체

## 변경 요약

| 파일 | 변경 |
|------|------|
| infrastructure/askpass.py | shim 파일 생성(.bat/.sh) 제거 → `CREDENTIAL_HELPER` 문자열 + `credential_helper_config()` + `credential_environment()` |
| infrastructure/remote_engine.py | shim 임시 디렉터리 생성·정리 제거, helper `-c` 주입(체인 재설정 뒤), git 2.50의 `unable to get password from user`를 인증 요구로 분류, `GIT_TRACE2_EVENT_NESTING=10` (§3.11) |
| tests | shim 실행 테스트 → helper sh 실행 테스트(적대적 비밀번호 목록 유지), Windows 전용 .bat helper 픽스처 3곳을 크로스플랫폼 `!f()` sh helper로 |

## 검증 (2026-07-31, macOS · git 2.50.1)

| 인수 조건 | 검증 | 결과 |
|---|---|---|
| 인증 스위트 전부 통과 (실서버 왕복 포함) | test_auth.py 24개 + test_auth_hardening.py 20개 | **성공** (이전: 20개 실패) |
| 적대적 비밀번호 변형·유출 0, 파일 생성 0 | TestHelperHandlesHostileSecrets (7종 × 2 + 주입 + username) | 성공 |
| helper 명령에 비밀번호 없음 | TestSecrets·TestHelperCommand | 성공 |
| 저장 안 함·저장·재사용·거부 흐름 유지 | TestRememberIsHonoured 등 | 성공 |
| 협상 왕복 기록 (§3.11) | test_negotiation_rounds_are_measured | 성공 (이전: 실패) |

전체 스위트: **748 통과 / 3 실패 / 4 skip** — 남은 3건은 backlog §3.12
(진행 표시 테스트의 타이밍 의존, 계약 결정 필요)로 이 변경과 무관.

## 남은 사항

- **Windows 미검증** — `!` helper의 sh 실행은 Git for Windows 표준이나 실측
  필요. DCR-003 위험 항목.
- DCR-003·ADR-78은 사용자 사후 승인 대기 상태다 — 승인 시 상태를
  `승인`/`반영 완료`로 올리고 ADR-31 상태를 확정한다.
