"""벤치마크용 합성 저장소 생성기.

**중요: 반드시 팩된 저장소를 만든다.**

느슨한(loose) 오브젝트로 남는 저장소는 순회가 40배 느려 현실을 대표하지 못한다.
실제 저장소는 clone하면 팩된 상태로 오고 이후 git이 `gc`로 팩을 유지한다.
이 사실을 모르고 pygit2 루프로 fixture를 만들었다가 G2 미달이라는
잘못된 결론에 도달한 적이 있다. (doc/design.md §4.1.1.3)

`git fast-import`를 쓰는 이유는 두 가지다.
  1. 결과가 팩된 상태로 나온다 (현실을 대표한다)
  2. 빠르다 — 10만 커밋에 1.4초. pygit2 루프는 같은 작업에 20분이 걸린다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

BASE_TIME = 1_700_000_000
AUTHOR = "김개발 <dev@example.com>"
SIDE_AUTHOR = "이코딩 <coder@example.com>"


def resolve_side_every(commits: int, side_every: int | None) -> int:
    """사이드 브랜치 간격을 커밋 수에 비례해 정한다.

    고정값을 쓰면 작은 fixture에 머지가 하나도 생기지 않아, 그래프 렌더링
    비용이 측정에서 통째로 빠진다. 규모와 무관하게 분기가 생기도록 한다.
    """
    if side_every is not None:
        return side_every
    return max(20, min(1_000, commits // 20))


def build_stream(
    commits: int,
    *,
    side_every: int | None = None,
    side_length: int = 20,
) -> str:
    """fast-import 스트림을 만든다.

    주 히스토리 위에 주기적으로 사이드 브랜치가 갈라졌다 머지된다.
    레인 배치 알고리즘이 실제로 일을 하도록 만들기 위함이다.
    선형 히스토리만으로 측정하면 그래프 렌더링 비용이 과소평가된다.
    """
    side_every = resolve_side_every(commits, side_every)
    side_length = min(side_length, max(2, side_every // 2))
    out: list[str] = []
    w = out.append

    payload = "x\n"
    w("blob\n")
    w("mark :1\n")
    w(f"data {len(payload.encode())}\n")
    w(payload)
    w("\n")

    mark = 2
    main_mark: int | None = None
    total = 0
    ts = BASE_TIME

    def write_commit(
        ref: str, message: str, author: str, parent: int | None, merge: int | None = None
    ) -> int:
        nonlocal mark, ts
        ts += 60
        encoded = message.encode()
        w(f"commit {ref}\n")
        w(f"mark :{mark}\n")
        w(f"author {author} {ts} +0900\n")
        w(f"committer {author} {ts} +0900\n")
        w(f"data {len(encoded)}\n")
        w(message)
        w("\n")
        if parent is None:
            w("deleteall\n")
            w("M 100644 :1 f.txt\n")
        else:
            w(f"from :{parent}\n")
        if merge is not None:
            w(f"merge :{merge}\n")
        current = mark
        mark += 1
        return current

    while total < commits:
        main_mark = write_commit("refs/heads/main", f"커밋 {total}", AUTHOR, main_mark)
        total += 1

        if total % side_every == 0 and total + side_length + 1 < commits:
            side_parent = main_mark
            for i in range(side_length):
                side_parent = write_commit(
                    "refs/heads/side", f"사이드 {total}-{i}", SIDE_AUTHOR, side_parent
                )
                total += 1

            main_mark = write_commit(
                "refs/heads/main",
                f"Merge side at {total}",
                AUTHOR,
                main_mark,
                merge=side_parent,
            )
            total += 1

    w("done\n")
    return "".join(out)


def build_repository(
    path: Path,
    commits: int,
    *,
    side_every: int | None = None,
    write_commit_graph: bool = True,
) -> Path:
    """지정 경로에 팩된 bare 저장소를 만든다. 이미 있으면 그대로 재사용한다."""
    if (path / "objects").exists():
        return path

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )

    stream = build_stream(commits, side_every=side_every)
    proc = subprocess.run(
        ["git", "fast-import", "--done", "--quiet"],
        cwd=str(path),
        input=stream.encode("utf-8"),
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "fast-import 실패: " + proc.stderr.decode("utf-8", "replace")[:1000]
        )

    if write_commit_graph:
        subprocess.run(
            ["git", "commit-graph", "write", "--reachable"],
            cwd=str(path),
            check=True,
            capture_output=True,
        )

    return path


def count_loose_objects(path: Path) -> int:
    """느슨한 오브젝트 수. 0이어야 현실적인 fixture다."""
    objects = path / "objects"
    if not objects.exists():
        return 0
    total = 0
    for sub in objects.iterdir():
        if sub.is_dir() and len(sub.name) == 2:
            total += sum(1 for _ in sub.iterdir())
    return total


def count_packs(path: Path) -> int:
    pack_dir = path / "objects" / "pack"
    if not pack_dir.exists():
        return 0
    return sum(1 for _ in pack_dir.glob("*.pack"))
