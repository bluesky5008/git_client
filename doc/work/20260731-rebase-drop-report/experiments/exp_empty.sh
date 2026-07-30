set -u
G="git -c user.name=t -c user.email=t@t"
mk() { d="$(mktemp -d)"; cd "$d"; $G init -qb main .; echo base > f.txt; echo other > g.txt; $G add -A; $G commit -qm base; }

echo "=========== 실험1: 충돌을 upstream 쪽으로 해결 → continue"
mk
git checkout -qb feat; echo feat > f.txt; $G commit -qam "feat-change"
git checkout -q main; echo up > f.txt; $G commit -qam "up-change"
git checkout -q feat
git rebase main 2>&1 | tail -2
git checkout --ours f.txt && git add f.txt   # rebase 중 ours = upstream 쪽
out=$(git rebase --continue </dev/null 2>&1); rc=$?
echo "continue rc=$rc"; echo "$out" | head -5

echo "=========== 실험2: 적용 시 비게 되는 커밋 (subset, 충돌 없음) — 기본값"
mk
git checkout -qb feat; echo new1 > f.txt; $G commit -qam "line1-only"
git checkout -q main; echo new1 > f.txt; echo extra > h.txt; $G add -A; $G commit -qm "line1-plus-more"
git checkout -q feat
out=$(git rebase main 2>&1); rc=$?
echo "rebase rc=$rc"; echo "$out" | tail -3
echo "결과 커밋 수(main..feat): $(git rev-list --count main..feat)"

echo "=========== 실험3: 같은 상황 + --empty=stop"
git rebase --abort 2>/dev/null
git reset -q --hard  # 실험2가 완료됐으면 되돌릴 수 없으니 새로 구성
mk
git checkout -qb feat; echo new1 > f.txt; $G commit -qam "line1-only"
git checkout -q main; echo new1 > f.txt; echo extra > h.txt; $G add -A; $G commit -qm "line1-plus-more"
git checkout -q feat
out=$(git rebase --empty=stop main 2>&1); rc=$?
echo "rebase rc=$rc"; echo "$out" | tail -4
echo "저장소 상태 파일:"; ls .git/rebase-merge/ 2>/dev/null | head; test -f .git/rebase-merge/stopped-sha && echo "stopped-sha: $(cat .git/rebase-merge/stopped-sha)"
echo "인덱스 충돌: $(git diff --name-only --diff-filter=U | wc -l | tr -d ' ')개"

echo "=========== 실험4: 이미 upstream에 그대로 반영된 커밋(clean cherry-pick) + --empty=stop"
mk
git checkout -qb feat; echo same > f.txt; $G commit -qam "identical-change"
git checkout -q main; git cherry-pick -x feat >/dev/null 2>&1 || git cherry-pick feat >/dev/null 2>&1
git checkout -q feat; echo more > g.txt; $G commit -qam "second"
out=$(git rebase --empty=stop main 2>&1); rc=$?
echo "rebase rc=$rc (0이어야 함 — clean cherry-pick은 --empty 대상 아님)"; echo "$out" | grep -i "drop\|skip" | head -3
echo "결과 커밋 수(main..feat): $(git rev-list --count main..feat) (1이어야 함)"
