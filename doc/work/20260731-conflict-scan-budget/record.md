# 작업 기록 — DCR-002 구현

## 기준선

- 관련 설계: DCR-002 (승인 2026-07-31), design.md v2.4, ADR-77 (ADR-47 대체)
- 관련 ADR: ADR-63·64·75 (일관 유지)

## 변경 요약

| 파일 | 변경 |
|------|------|
| domain/models.py | `ConflictedFile.has_markers: bool \| None` (None = 미분류) |
| infrastructure/local_engine.py | `_collect_conflicts(classify=)`로 열거/분류 분리, `_text_pair_has_markers()`(워커 전용), merge·시퀀서 결과는 `classify=True` |
| application/conflict_loader.py | 신규 — 충돌 상세를 세대 토큰 방식으로 워커에서 읽는다 (DiffLoader 패턴 재사용) |
| ui/main_window.py | 상세 요청을 로더로, 최근 상세 캐시를 `_working_copy_edited`가 재사용(3배→1배), markerless 판정 `is False`로 정밀화 |
| ui/conflict_panel.py | 선택 직후 버튼 잠금 + "읽는 중" 표시 |

## 검증 (2026-07-31, macOS · git 2.50.1 · Python 3.12.13)

| 인수 조건 | 검증 | 결과 |
|---|---|---|
| 열거가 G4 예산(50ms) 안 | test_enumeration_fits_the_g4_budget (충돌 200×2,000줄) | 성공 |
| 열거가 blob을 열지 않음(구조) | test_enumeration_leaves_text_pairs_unclassified | 성공 |
| 삭제 계열은 즉시 False | test_deletion_conflicts_are_classified_by_kind_alone | 성공 |
| 워커 결과는 분류돼 옴 | 기존 test_merge_hardening (has_markers True/False) 유지 통과 | 성공 |
| 실패 시 이전 파일 잔류 금지 | test_failed_detail_...(클래스 레벨 주입으로 갱신) 통과 | 성공 |
| stash pop 충돌 회귀 없음 | 기존 스위트 유지 통과 | 성공 |

사전 실측(설계 근거): 분류 포함 26/144/2,957ms → 열거만 1.2/5.9/1.4ms
(experiments/exp_scan.py). 벤치마크 재실행: 첫 행 599ms(G2), 단일 정지
2.0ms(G4) — 회귀 없음.

## 남은 사항

- 해결 버튼 클릭 시 워킹 파일 1회 읽기는 UI 스레드에 남는다(클릭 시점
  내용이어야 하는 편집 판정) — backlog §3.3에 🟢로 축소 기록.
- 인덱스 자체가 수십만 항목인 극단 저장소의 열거 비용은 미측정 — 필요 시
  전면 워커화(기각한 대안)로 확장 가능한 구조다.
