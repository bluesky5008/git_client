import subprocess, sys, tempfile, time
from pathlib import Path
sys.path.insert(0, "src")
import pygit2
from gitclient.infrastructure.local_engine import LocalGitEngine

def G(*a, cwd): subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=True, capture_output=True)

for n_files, lines in [(200, 100), (1000, 100), (200, 20000)]:
    tmp = Path(tempfile.mkdtemp()); root = tmp / "r"; root.mkdir()
    G("init", "-qb", "main", ".", cwd=root)
    body = ("x" * 60 + "\n") * lines
    for i in range(n_files): (root / f"f{i}.txt").write_text("base\n" + body)
    G("add", "-A", cwd=root); G("commit", "-qm", "base", cwd=root)
    G("checkout", "-qb", "feat", cwd=root)
    for i in range(n_files): (root / f"f{i}.txt").write_text("feat\n" + body)
    G("commit", "-qam", "feat", cwd=root)
    G("checkout", "-q", "main", cwd=root)
    for i in range(n_files): (root / f"f{i}.txt").write_text("main\n" + body)
    G("commit", "-qam", "main", cwd=root)
    r = subprocess.run(["git", "merge", "feat"], cwd=root, capture_output=True)
    assert r.returncode != 0

    eng = LocalGitEngine.open(str(root))
    t0 = time.perf_counter(); full = eng.index_conflicts(); t1 = time.perf_counter()

    # 마커 분류 없이 경로·side만 열거 (대안 A의 동기 경로 시뮬레이션)
    repo = pygit2.Repository(str(root))
    t2 = time.perf_counter()
    idx = pygit2.Index(str(Path(repo.path) / "index")); idx.read()
    paths = [( (o or t or a).path ) for a, o, t in (idx.conflicts or [])]
    t3 = time.perf_counter()
    print(f"충돌 {n_files}개 × {lines}줄: 현행 전체 스캔 {1000*(t1-t0):.0f}ms | 경로만 열거 {1000*(t3-t2):.1f}ms (파일 {len(paths)}개)")
