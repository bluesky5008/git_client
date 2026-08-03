# 작업 기록 — 저장소 전환 시 이전 HEAD 조회 오류 (실사용 버그)

## 기준선

- 경량 경로 (wf-implement §1 예외): 단일 결함, 국소·가역, 새 설계 결정 없음
- 사용자 보고: A 사용 → B 열기 → "커밋을 찾을 수 없습니다: <A의 HEAD>"

## 원인 (재현 스크립트 + 스택 추적으로 확정)

`open_repository()`의 초기화 순서 결함:

```
:840 _repo_path = B                ← 새 경로가 먼저 들어가고
:848 _show_placeholder()
      └ _file_list.clear()         ← 항목 제거 중 currentRowChanged 발화
           └ _on_file_selected     ← 가드 셋(row·sha·경로) 전부 통과
                └ _request_diff(_current_sha ← 아직 A의 HEAD)
:849 _current_sha = None           ← 한 줄 늦었다
```

새 엔진(B)에 A의 sha를 조회 → `_lookup_commit` 실패 → 오류 보고.
**핸들러가 소비하는 상태를 무효화하기 전에, 그 핸들러를 발화시키는
위젯을 건드린 것**이 근본 원인이다.

## 수정

`_current_sha = None`을 위젯 정리 블록 **앞**으로 — 한 줄 이동.
이후 어떤 위젯이 시그널을 내든 sha 가드가 막는다.

## 검증

| 항목 | 방법 | 결과 |
|---|---|---|
| 재현 | 스크립트: A 열기→커밋 선택→B 열기 | 수정 전 오류 발생, 수정 후 소멸 |
| 회귀 테스트 | test_switching_does_not_query_the_old_head — stale **요청 자체**를 기록해 단언 (오류 보고 대기는 세대 토큰이 늦은 실패를 버려 비결정적임을 확인하고 폐기) | 결함 재주입 시 결정적 실패, 수정 시 통과 |
| 전체 | pytest | 866 통과 / 4 skip / 0 실패 |
