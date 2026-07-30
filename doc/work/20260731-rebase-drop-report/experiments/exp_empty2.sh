set -u
G="git -c user.name=t -c user.email=t@t"
mk() { d="$(mktemp -d)"; cd "$d"; $G init -qb main .; echo base > f.txt; echo other > g.txt; $G add -A; $G commit -qm base; }

echo "=========== 실험5: --empty=stop + 충돌을 upstream 쪽으로 해결 → continue"
mk
git checkout -qb feat; echo feat > f.txt; $G commit -qam "feat-change"
git checkout -q main; echo up > f.txt; $G commit -qam "up-change"
git checkout -q feat
git rebase --empty=stop main >/dev/null 2>&1
git checkout --ours f.txt && git add f.txt
out=$(git rebase --continue </dev/null 2>&1); rc=$?
echo "continue rc=$rc"
echo "$out" | head -4
echo "결과 커밋 수(main..feat): $(git rev-list --count main..feat)"

echo "=========== 실험6: continue 직전에 '빈 커밋이 될 것'을 앱이 판정할 수 있는가"
mk
git checkout -qb feat; echo feat > f.txt; $G commit -qam "feat-change"
git checkout -q main; echo up > f.txt; $G commit -qam "up-change"
git checkout -q feat
git rebase main >/dev/null 2>&1
git checkout --ours f.txt && git add f.txt
# 판정: 스테이징된 트리 == HEAD 트리 ?
staged=$(git write-tree)
head_tree=$(git rev-parse HEAD^{tree})
echo "staged tree == HEAD tree: $([ "$staged" = "$head_tree" ] && echo YES — 빈 커밋 예정 || echo NO)"
git rebase --abort

echo "=========== 실험7: cherry-pick에도 같은 판정이 서는가 (충돌→upstream 해결)"
mk
git checkout -qb feat; echo feat > f.txt; $G commit -qam "feat-change"
git checkout -q main; echo up > f.txt; $G commit -qam "up-change"
git cherry-pick feat >/dev/null 2>&1   # main 위에서 feat을 pick → 충돌
git checkout --ours f.txt && git add f.txt
staged=$(git write-tree); head_tree=$(git rev-parse HEAD^{tree})
echo "staged tree == HEAD tree: $([ "$staged" = "$head_tree" ] && echo YES || echo NO)"
out=$(git cherry-pick --continue </dev/null 2>&1); rc=$?
echo "cherry-pick --continue rc=$rc"; echo "$out" | head -3
