# DCR-001: rebase의 커밋 소실을 "조용한 성공"에서 "명시적 선택"으로

- 상태: 반영 완료 (승인 2026-07-31 사용자, 구현·검증 2026-07-31 — [record.md](record.md))
- 발견 위치: backlog.md §3.1 (Phase 4 증분 3 리뷰가 남긴 🔴, "막힌 지점" 상태)
- 현재 설계 기준선: design.md v2.2 (§4.12), ADR-65·73
- 관련 요구사항: G1(워크플로 GUI 완결) — 파괴적 결과를 사용자가 모르게 하지 않는다
- 관련 ADR: ADR-65(해소됨 — 이 DCR은 그 잔재를 다룬다), ADR-69(성패는 저장소 상태로 판정), ADR-73(파괴적 동작 확인)

## 변경 사유와 증거

리베이스에서 충돌을 upstream 쪽으로 해결하면 그 커밋에 남는 변경이 없어지고,
git은 커밋을 조용히 버린 채 "Successfully rebased"로 끝낸다. 앱은 그대로
`COMPLETED`를 보고한다 — 사용자는 로그를 직접 보기 전에는 커밋 하나가
사라진 것을 알 수 없다.

backlog §3.1은 이 문제를 **사후 집계**(끝난 뒤 "N개가 결과에 남지 않았다"를
세기)로 접근했고, `ORIG_HEAD` 비교가 정당한 생략(이미 upstream에 반영된
커밋)과 구분되지 않아 막혔다.

**2026-07-31 실측(git 2.50.1, macOS)으로 접근을 바꿀 근거가 확인됐다.**
소실 경로는 하나가 아니라 셋이고, 각각 성질이 다르다:

| # | 경로 | git 동작 (실측) | 성질 |
|---|------|----------------|------|
| (a) | 이미 upstream에 그대로 반영된 커밋 (clean cherry-pick) | 항상 조용히 생략, stderr에 `warning: skipped previously applied commit <sha>` | **정당한 생략** — 잃는 것이 없다 |
| (b) | 충돌 없이 적용 시점에 비게 되는 커밋 (변경이 upstream의 부분집합) | 기본값에서 조용히 버림 (`dropping <sha> ... patch contents already upstream`, rc=0). `--empty=stop`을 주면 **멈춘다** (rc=1, `stopped-sha` 기록, 인덱스 충돌 0개) | 대개 무해하나 사용자가 몰라선 안 됨 |
| (c) | 충돌을 upstream 쪽으로 해결 → `--continue` | **`--empty=stop`을 줘도 조용히 버리고 rc=0 "Successfully rebased"** | 🔴 §3.1의 핵심 소실. cherry-pick은 같은 상황에서 스스로 거부하는데(`The previous cherry-pick is now empty…`, rc=1) rebase만 버린다 |

그리고 (c)를 **사전에** 잡는 결정론적 판정이 실측으로 확인됐다:

```
# 충돌 해결을 스테이징한 직후, --continue를 부르기 전에
git write-tree == git rev-parse HEAD^{tree}   →  YES = 이번 커밋은 빈 커밋이 된다
```

스테이징된 트리가 HEAD의 트리와 같으면 재생될 커밋에 남는 변경이 없다는
뜻이다 — 추측이 아니라 정의다. 앱은 인덱스를 이미 읽고 있으므로
(`continue_operation()`이 남은 충돌을 검사한다, local_engine.py:1911)
같은 자리에서 트리 비교 한 번이면 된다. pygit2 `index.write_tree()`로
로컬에서 끝나며 비용은 인덱스 크기에 비례하는 밀리초 수준이다.

재현 스크립트: 실험 1~7 (`exp_empty.sh`, `exp_empty2.sh` — 이 디렉터리의
`experiments/`에 보존).

## 기존 설계

- `HistoryOutcome`은 `COMPLETED | CONFLICTED` 둘뿐이고, "커밋이 결과에
  남지 않았다"를 실을 자리가 없다 (models.py:615).
- `continue_operation()`은 남은 충돌만 검사하고 곧장 `--continue`를 부른다.
- backlog §3.1은 사후 집계 방식이 막혀 "반쯤 맞는 휴리스틱을 넣지 않았다"로
  보류 상태다.

## 제안 설계

사후 집계를 버리고, **소실이 일어나는 시점마다 그 자리에서 막거나 알린다.**
세 경로에 각각 대응한다:

1. **(c) continue 직전 판정 — 핵심.** `continue_operation()`이 남은 충돌
   검사 다음에 `index.write_tree() == HEAD.tree`를 확인한다. 같으면
   `--continue`를 부르지 않고 새 결과 `HistoryOutcomeKind.WOULD_BE_EMPTY`를
   돌려준다. UI는 명시적 선택지를 띄운다:
   - **건너뛰기** — 기존 `skip_operation()` 경로 (ADR-73의 확인·버튼 잠금 그대로)
   - **빈 커밋으로 유지** — `git commit --allow-empty`(시퀀서가 메시지를
     보존한다) 후 `--continue`
   - **중단** — 기존 `abort_operation()`
2. **(b) `--empty=stop` 추가.** rebase 명령에 `--empty=stop`을 더한다.
   멈추면 저장소 상태는 "진행 중 + 인덱스 충돌 0개"인데, 현재 엔진은
   충돌 0개인 멈춤을 오류로 바꾼다(HistoryOutcomeKind.CONFLICTED 주석).
   이 상태를 오류가 아니라 위 1과 같은 `WOULD_BE_EMPTY`로 돌려준다 —
   선택지도 같다.
3. **(a) 정당한 생략은 알림으로.** stderr의
   `skipped previously applied commit <sha>` 경고를 파싱해
   `HistoryOutcome.skipped_already_applied: tuple[str, ...]`에 싣는다.
   UI는 완료 보고에 "N개는 이미 upstream에 있어 생략했습니다"를 덧붙인다.
   (로케일은 이미 `LC_ALL=C`로 고정되어 있어 파싱이 안정적이다 — §4.6.)

이 설계에는 휴리스틱이 없다. (a)와 (b)·(c)를 혼동할 여지가 없고 —
(a)는 애초에 멈추지 않고 경고 줄로 구분된다 — backlog가 우려한
"평범한 리베이스마다 거짓 경고"는 구조적으로 발생하지 않는다.

## 변경 항목

| 위치 | 기존 | 변경 | 이유 |
|------|------|------|------|
| `HistoryOutcomeKind` | COMPLETED·CONFLICTED | `WOULD_BE_EMPTY` 추가, `skipped_already_applied` 필드 추가 | 소실 직전 상태와 정당한 생략을 실을 자리 |
| `continue_operation()` | 충돌 검사 후 즉시 `--continue` | 트리 비교로 빈 커밋 예정을 먼저 판정 | (c)를 사전에 차단 |
| `rebase()` | `["rebase", "--", upstream]` | `--empty=stop` 추가 | (b)를 사용자 선택으로 |
| `_sequencer` 상태 해석 | 충돌 0개 멈춤 = 오류 | 충돌 0개 + 진행 중 = `WOULD_BE_EMPTY` | (b)의 멈춤은 오류가 아니라 선택 대기 |
| main_window | — | `WOULD_BE_EMPTY` 다이얼로그(건너뛰기/유지/중단), 완료 보고에 생략 안내 | 선택은 사용자 몫 |

## 영향 범위

- 공개 인터페이스: `HistoryOutcome` 도메인 모델 확장 (하위 호환 — 기존 값 의미 불변)
- 기존 ADR과의 관계: ADR-69(상태로 판정)·ADR-73(파괴적 확인)과 같은 방향.
  충돌하는 ADR 없음. ADR-65의 잔재 항목을 종결한다.
- cherry-pick·revert: git이 스스로 거부하므로(실측 7) 변경 불필요.
  단, 그 거부 메시지를 `WOULD_BE_EMPTY`로 번역하면 UX가 rebase와 일관된다 — 선택 사항.

## 구현된 코드의 처리

기존 구현은 전부 유지된다. 이 변경은 추가이며 되돌릴 것이 없다.

## 마이그레이션 및 롤백

저장소·데이터 마이그레이션 없음. 롤백은 코드 되돌리기로 충분하다.
`--empty=stop`은 git 2.29+에서 지원된다(요구사항 git 2.40+ 안).

## 검증 방법

- 실험 1·5 시나리오를 통합 테스트로: 충돌을 upstream으로 해결 → 계속 →
  `WOULD_BE_EMPTY`가 돌아오고 커밋이 사라지지 않는다 (AC: 소실 0)
- 실험 2·3 시나리오: 부분집합 커밋 → rebase가 멈추고 선택지가 온다
- 실험 4 시나리오: clean cherry-pick 생략 → 완료 + 생략 안내 N=1,
  거짓 경고 없음 (AC: 평범한 리베이스에서 `WOULD_BE_EMPTY` 미발생)
- 세 선택지 각각의 사후 상태 (건너뛰기 → 커밋 수 -1, 유지 → 빈 커밋 존재,
  중단 → ORIG_HEAD 복원)
