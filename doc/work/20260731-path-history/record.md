# 작업 기록 — 경로 히스토리 + commit-graph 측정·결정

## 기준선

- 요구사항·설계: [requirements-and-design.md](requirements-and-design.md)
  (사용자 승인 2026-07-31, AskUserQuestion "승인")
- 관련 ADR: ADR-90 (신규 — CLI 채택 + idle_repack의 Bloom 쓰기)

## 측정 (설계 §3.1의 프로토타입, macOS arm64 · git 2.50.1)

1만 커밋 합성 저장소(대상 파일 변경 50회), 5회 중앙값:

| 후보 | 완주 |
|------|------|
| A. pygit2 순회 | 366.7ms |
| B. CLI, Bloom 없음 | 58.7ms |
| C. CLI, Bloom 있음 | **22.9ms** |

commit-graph 쓰기 137.7ms(1회) — 유휴라 공짜. **C 채택.** A는 속도에서
지는 것에 더해 git의 히스토리 단순화(TREESAME)를 재구현해야 맞는 결과가
나온다 — 정확성 표면에서도 진다. 재현: `tests/benchmarks/bench_path_history.py`

## 구현

- `LocalGitEngine.path_history(path, limit=1000)` — `git log --format=%H%x1f…`
- `idle_repack()` — repack 직후 `commit-graph write --reachable --changed-paths`
  (`_idle_command` 헬퍼로 분리, 중단 0.2초 응답 유지)
- `PathHistoryLoader` + `PathHistoryDialog`(목록+diff, DiffLoader 경로 좁히기
  재사용), 변경 파일 목록 우클릭 "이 파일의 히스토리"
- `drops_late_deliveries`를 `ui/late_delivery.py`로 승격 — 다이얼로그도 같은
  수명 문제를 가진다
- 카탈로그 +10 (정적 6·템플릿 3·오류 1)

## 검증 (AC 대응)

| AC | 검증 | 결과 |
|---|---|---|
| AC-01·02 경로 필터 = git log | 병합 포함 저장소에서 CLI 출력과 sha 열 대조 | 성공 |
| AC-03 UI 스레드 비차단 | 로더 패턴 + 다이얼로그 종단 3건 (도착·빈 목록·닫힘 안전) | 성공 |
| Bloom 실재 | idle_repack 후 BIDX 청크 존재 단언 (파일 유무만 보면 --changed-paths 누락이 통과한다) | 성공 |

## 남은 사항

- rename 추적 (FR-04 범위 밖 — 화면이 한계를 밝힘)
