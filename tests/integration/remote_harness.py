"""원격 작업 테스트 하네스.

로컬 bare 저장소를 원격으로 삼는다 — 네트워크에 의존하지 않으면서 팩
프로토콜을 실제로 태운다. (doc/design.md §8의 '네트워크' 행)

**`file://` URI를 쓰는 이유**: 평범한 로컬 경로를 주면 git이 팩 프로토콜을
건너뛰는 지름길을 타서 `Receiving objects` 진행률이 나오지 않는다. 그러면
계측 경로가 테스트되지 않는다. URI를 쓰면 실제 원격과 같은 경로를 탄다.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

AUTHOR_ENV = (
    "-c", "user.name=테스터",
    "-c", "user.email=tester@example.com",
)


def git(*args: str, cwd: Path | str | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise AssertionError(
            f"하네스의 git 명령이 실패했다: {' '.join(args)}\n{result.stderr}"
        )
    return result


class RemoteFixture:
    """원격 역할 bare 저장소 + 그것을 clone한 작업 저장소."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.origin = root / "origin.git"
        self.seed = root / "seed"
        self.work = root / "work"

    @property
    def origin_uri(self) -> str:
        """git에 넘길 원격 주소. 팩 프로토콜을 타도록 file:// 로 준다."""
        return self.origin.resolve().as_uri()

    def build(self, commits: int = 5, payload_kb: int = 1) -> RemoteFixture:
        git("init", "--bare", "-b", "main", str(self.origin))
        git("init", "-b", "main", str(self.seed))
        self.add_commits(commits, payload_kb=payload_kb)
        self.publish()
        git("clone", "--quiet", self.origin_uri, str(self.work))
        return self

    def add_commits(
        self, count: int, *, payload_kb: int = 1, prefix: str = "f"
    ) -> None:
        """seed 저장소에 커밋을 쌓는다. 아직 원격에는 반영되지 않는다."""
        payload = ("x" * 1024 + "\n") * payload_kb
        existing = len(list(self.seed.glob(f"{prefix}*.txt")))
        for index in range(count):
            number = existing + index
            (self.seed / f"{prefix}{number}.txt").write_text(
                payload + str(number), encoding="utf-8"
            )
            git("add", "-A", cwd=self.seed)
            git(*AUTHOR_ENV, "commit", "--quiet", "-m", f"커밋 {number}", cwd=self.seed)

    def publish(self) -> None:
        """seed의 커밋을 원격에 올린다 — 작업 저장소가 fetch할 거리를 만든다."""
        git("push", "--quiet", str(self.origin), "main", cwd=self.seed)

    def sync_seed(self) -> None:
        """seed를 원격의 현재 상태로 맞춘다.

        push를 검증하면서 필요해졌다. 작업 저장소가 원격에 올리고 나면 seed는
        원격보다 뒤처지므로, 그 상태에서 커밋을 쌓아 publish하면 비빨리감기로
        거부된다 — 만들려던 상황과 무관한 실패다.

        seed는 원격 쪽 커밋을 지어내기 위한 도구용 저장소이므로 여기서
        되감아도 잃을 사용자 작업이 없다.
        """
        git("fetch", "--quiet", str(self.origin), "main", cwd=self.seed)
        git("reset", "--hard", "--quiet", "FETCH_HEAD", cwd=self.seed)

    def add_and_publish(self, count: int = 1, *, payload_kb: int = 1) -> None:
        self.sync_seed()
        self.add_commits(count, payload_kb=payload_kb)
        self.publish()

    def create_remote_branch(self, name: str) -> None:
        """원격에 브랜치를 만든다.

        seed의 현재 main을 가리키므로 작업 저장소가 이미 그 커밋을 갖고 있다 —
        즉 **객체 전송 없이 참조만 늘어나는** fetch를 만들어낸다. 계측이
        "받은 객체 수"로 변경 여부를 판단하면 놓치는 경우다.
        """
        git("branch", name, cwd=self.seed)
        git("push", "--quiet", str(self.origin), name, cwd=self.seed)

    def delete_remote_branch(self, name: str) -> None:
        """원격에서 브랜치를 지운다 — 작업 저장소의 prune 거리를 만든다."""
        git("branch", "-D", name, cwd=self.seed)
        git("push", "--quiet", str(self.origin), "--delete", name, cwd=self.seed)

    def force_push_rewrite(self) -> None:
        """원격 히스토리를 갈아엎는다 — fetch가 '강제 갱신' 형태로 보고한다.

        이때 git은 "a...b"(점 세 개)를 내는데, 빨리 감기의 "a..b"와 형태가
        달라 파서가 놓치기 쉽다.
        """
        git(*AUTHOR_ENV, "commit", "--quiet", "--amend", "-m", "재작성", cwd=self.seed)
        git("push", "--quiet", "--force", str(self.origin), "main", cwd=self.seed)

    def create_remote_tag(self, name: str) -> None:
        git(*AUTHOR_ENV, "tag", "-a", name, "-m", name, cwd=self.seed)
        git("push", "--quiet", str(self.origin), name, cwd=self.seed)

    # ------------------------------------------------------------------
    # push 쪽 거리 만들기
    # ------------------------------------------------------------------

    def commit_locally(self, count: int = 1, *, payload_kb: int = 1) -> None:
        """**작업 저장소**에 커밋을 쌓는다 — push할 거리를 만든다.

        `add_commits`는 seed(원격 쪽)에 쌓는다는 점에서 방향이 반대다.
        """
        payload = ("y" * 1024 + "\n") * payload_kb
        existing = len(list(self.work.glob("local*.txt")))
        for index in range(count):
            number = existing + index
            (self.work / f"local{number}.txt").write_text(
                payload + str(number), encoding="utf-8"
            )
            git("add", "-A", cwd=self.work)
            git(
                *AUTHOR_ENV, "commit", "--quiet", "-m", f"로컬 커밋 {number}",
                cwd=self.work,
            )

    def diverge(self) -> None:
        """원격과 작업 저장소가 서로 다른 커밋을 갖게 만든다.

        push는 거부되고 pull은 병합이 필요한 상태 — 협업에서 가장 흔한
        마찰 지점이라 두 경로 모두 이 상황을 다뤄야 한다.
        """
        self.add_and_publish(1)
        self.commit_locally(1)

    def origin_branch_head(self, name: str = "main") -> str:
        return git("rev-parse", name, cwd=self.origin).stdout.strip()

    def origin_has_branch(self, name: str) -> bool:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", f"refs/heads/{name}"],
            cwd=str(self.origin), capture_output=True, text=True,
        )
        return result.returncode == 0

    def work_head(self) -> str:
        return git("rev-parse", "HEAD", cwd=self.work).stdout.strip()

    def work_remote_head(self, remote: str = "origin") -> str:
        return git(
            "rev-parse", f"refs/remotes/{remote}/main", cwd=self.work
        ).stdout.strip()

    def origin_head(self) -> str:
        return git("rev-parse", "main", cwd=self.origin).stdout.strip()
