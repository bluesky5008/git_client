set -u
# 이 저장소 자신의 히스토리로 잰다 — 기본 repack vs 델타 250/250
src="/Users/yongs/claude/git_client"
for mode in default delta; do
  d="$(mktemp -d)"
  git clone -q --no-local "$src" "$d/r" 2>/dev/null
  cd "$d/r"
  if [ "$mode" = delta ]; then
    t0=$(python3 -c 'import time;print(time.time())')
    git -c pack.window=250 -c pack.depth=250 repack -a -d -q
  else
    t0=$(python3 -c 'import time;print(time.time())')
    git repack -a -d -q
  fi
  t1=$(python3 -c 'import time;print(time.time())')
  size=$(du -sk .git/objects/pack | cut -f1)
  echo "$mode: ${size}KB, $(python3 -c "print(f'{($t1-$t0)*1000:.0f}ms')")"
done
