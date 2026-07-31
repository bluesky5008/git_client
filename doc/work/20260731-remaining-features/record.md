# 작업 기록 — backlog 권장 순서 1~3 (F1·F2·F3)

## 기준선

- 요구사항·설계: [requirements-and-design.md](requirements-and-design.md) v1
  (사용자 지시 "권장순서 1~3 순서대로 진행"으로 범위·착수 승인)
- 관련 ADR: ADR-85 (파일시스템 감시 — 신규)

## 계획 진행 상태

- [x] F1 원격 관리 — 추가·삭제·주소 변경 (pygit2 로컬 쓰기, WriteQueue)
- [x] F2 파일시스템 감시 — QFileSystemWatcher + `.git` 신호 3개 + 창 활성화
- [x] F3 줄 단위 충돌 해결 — 마커 파싱·구획 선택·조립·스테이징

## 검증 (2026-07-31, macOS · git 2.50.1)

| AC | 검증 | 결과 |
|---|---|---|
| AC-01 원격 관리는 동작으로 | test_remote_management 6건 — 추가한 원격으로 실제 fetch, 삭제 후 추적 참조 0, 주소 변경 후 새 목적지 fetch, 중복·부재 거부, 창 경유 종단 | 성공 |
| AC-02 외부 조작 자동 반영 | test_fs_watch 7건 — **진짜 파일시스템 이벤트로** 외부 커밋이 그래프에 반영, 자기 쓰기 도장·바쁨 가드, 활성화 시 상태 재로딩 | 성공 |
| AC-03 구획별 선택 | test_conflict_text 10건(diff3·훼손 거부·개행 왕복 포함) + test_line_resolution 7건(혼합 선택·스테이징·훼손 거부·창 경유) | 성공 |

전체 스위트 **820 통과 / 4 skip / 실패 0**.

## 남은 사항

- Windows 재검증 (backlog §1 1순위로 승격)
- 원격 이름 변경(rename)은 범위 밖 — 추가+삭제로 대체 가능
