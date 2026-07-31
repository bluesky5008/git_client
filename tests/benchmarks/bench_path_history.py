# -*- coding: utf-8 -*-
"""경로 히스토리 엔진 후보 실측 (doc/work/20260731-path-history §3.1, ADR-90).

pytest가 아니라 손으로 돌리는 기록용 스크립트다:
    .venv/bin/python tests/benchmarks/bench_path_history.py /tmp/bench-repo

2026-07-31 macOS(arm64)·git 2.50.1 실측 (중앙값 5회):
    A pygit2 순회        :    366.7ms  (50건)
    B CLI, Bloom 없음    :     58.7ms  (50건)
    C CLI, Bloom 있음    :     22.9ms  (50건)
    commit-graph 쓰기    :    137.7ms  (1회, 유휴에서)
→ CLI 채택, idle_repack이 --changed-paths를 쓴다.

후보:
  A. pygit2 순회 — 커밋마다 경로의 blob OID를 부모와 비교
  B. git CLI `log -- <path>` — commit-graph 없음
  C. git CLI `log -- <path>` — commit-graph + Bloom(--changed-paths)

저장소: 1만 커밋, 커밋마다 src/f{i%100}.txt 수정, 200번마다
src/deep/nested/target.txt 도 수정(대상 변경 50회).
"""
import subprocess, sys, time, statistics
from pathlib import Path

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/bench-path-history")
N = 10_000
TARGET = "src/deep/nested/target.txt"


def git(*args, **kw):
    return subprocess.run(["git", "-C", str(root), *args],
                          check=True, capture_output=True, text=True, **kw)


def build():
    if root.exists():
        return
    root.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    lines = []
    mark = 0
    for i in range(N):
        mark += 1
        blob_mark = mark
        content = f"content {i:012d}\n"
        lines.append(f"blob\nmark :{blob_mark}\ndata {len(content)}\n{content}")
        mark += 1
        lines.append(
            f"commit refs/heads/main\nmark :{mark}\n"
            f"author A <a@x> {1500000000+i} +0000\n"
            f"committer A <a@x> {1500000000+i} +0000\n"
            f"data {len(f'commit {i:03d}')}\ncommit {i:03d}\n"
            + (f"from :{mark-2}\n" if i else "")
            + f"M 100644 :{blob_mark} src/f{i%100}.txt\n"
            + (f"M 100644 :{blob_mark} {TARGET}\n" if i % 200 == 0 else "")
        )
    subprocess.run(["git", "-C", str(root), "fast-import", "--quiet"],
                   input="".join(lines), text=True, check=True,
                   capture_output=True)
    git("config", "core.commitGraph", "true")


def timeit(fn, reps=5):
    times = []
    for _ in range(reps):
        t0 = time.perf_counter()
        n = fn()
        times.append((time.perf_counter() - t0) * 1000)
    return statistics.median(times), n


def cand_a():
    import pygit2
    repo = pygit2.Repository(str(root))

    def oid_at(tree, parts):
        node = tree
        for p in parts:
            if node is None or p not in node:
                return None
            node = repo[node[p].id] if p != parts[-1] else node[p].id
        return node

    parts = TARGET.split("/")

    def entry(commit):
        node = commit.tree
        for p in parts[:-1]:
            if p not in node:
                return None
            node = repo[node[p].id]
        return node[parts[-1]].id if parts[-1] in node else None

    hits = 0
    for commit in repo.walk(repo.head.target, pygit2.GIT_SORT_TOPOLOGICAL):
        mine = entry(commit)
        parents = commit.parents
        if not parents:
            if mine is not None:
                hits += 1
        elif all(mine != entry(p) for p in parents):
            hits += 1
    return hits


def cand_cli():
    out = git("log", "--format=%H", "--", TARGET)
    return len(out.stdout.splitlines())


build()

# 커밋 그래프 제거 상태에서 A·B
cg = root / ".git" / "objects" / "info"
subprocess.run(["rm", "-rf", str(cg / "commit-graph"), str(cg / "commit-graphs")])
ms_a, n_a = timeit(cand_a)
ms_b, n_b = timeit(cand_cli)

# Bloom 쓰고 C
t0 = time.perf_counter()
git("commit-graph", "write", "--reachable", "--changed-paths")
write_ms = (time.perf_counter() - t0) * 1000
ms_c, n_c = timeit(cand_cli)

print(f"A pygit2 순회        : {ms_a:8.1f}ms  ({n_a}건)")
print(f"B CLI, Bloom 없음    : {ms_b:8.1f}ms  ({n_b}건)")
print(f"C CLI, Bloom 있음    : {ms_c:8.1f}ms  ({n_c}건)")
print(f"commit-graph 쓰기    : {write_ms:8.1f}ms (1회, 유휴에서)")
