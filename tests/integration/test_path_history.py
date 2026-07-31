"""경로 히스토리 (ADR-90).

엔진 계층의 계약은 하나다 — **`git log -- <경로>`와 같은 결과**. 히스토리
단순화(TREESAME 가지치기)를 우리가 재구현하지 않고 CLI에 맡긴 이유가
그것이므로, 검증도 CLI 출력과의 대조가 본체다 (AC-02).

UI 계층은 "워커가 읽어 오고 화면은 도착분을 그린다"의 종단을 본다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.infrastructure.local_engine import LocalGitEngine
from tests.integration.remote_harness import AUTHOR_ENV, git

TIMEOUT = 60_000


def commit_all(repo: Path, message: str) -> None:
    git("add", "-A", cwd=repo)
    git(*AUTHOR_ENV, "commit", "--quiet", "-m", message, cwd=repo)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """대상 파일을 3번 바꾸고, 다른 파일만 바꾼 커밋과 병합을 섞은 저장소.

    병합이 들어가는 이유: 경로 필터의 결과가 갈라지는 곳이 정확히
    병합의 단순화 규칙이다 — 직선 히스토리만 검증하면 CLI 대조가
    아무것도 대조하지 않는다.
    """
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    (root / "target.txt").write_text("v1\n", encoding="utf-8")
    (root / "other.txt").write_text("a\n", encoding="utf-8")
    commit_all(root, "target v1")

    (root / "other.txt").write_text("b\n", encoding="utf-8")
    commit_all(root, "other only")

    git("checkout", "--quiet", "-b", "side", cwd=root)
    (root / "target.txt").write_text("v2-side\n", encoding="utf-8")
    commit_all(root, "target v2 on side")

    git("checkout", "--quiet", "main", cwd=root)
    (root / "other.txt").write_text("c\n", encoding="utf-8")
    commit_all(root, "other again")
    git(*AUTHOR_ENV, "merge", "--quiet", "--no-edit", "side", cwd=root)

    (root / "target.txt").write_text("v3\n", encoding="utf-8")
    commit_all(root, "target v3")
    return root


class TestEngineMatchesGitLog:
    def test_only_commits_that_touch_the_path(self, repo: Path) -> None:
        """AC-01 + AC-02 — CLI와 같은 커밋을 같은 순서로."""
        engine = LocalGitEngine.open(str(repo))

        entries = engine.path_history("target.txt")

        expected = git(
            "log", "--format=%H", "--", "target.txt", cwd=repo
        ).stdout.split()
        assert [e.sha for e in entries] == expected
        assert all("other" not in e.summary for e in entries)

    def test_entry_carries_what_the_screen_draws(self, repo: Path) -> None:
        entry = LocalGitEngine.open(str(repo)).path_history("target.txt")[0]

        assert entry.summary == "target v3"
        assert entry.author  # 하네스의 AUTHOR_ENV가 정한 이름
        assert entry.when.year >= 2020
        assert len(entry.sha) == 40

    def test_unknown_path_is_empty_not_an_error(self, repo: Path) -> None:
        """git log의 의미론 그대로 — 없는 경로는 빈 목록이다."""
        assert LocalGitEngine.open(str(repo)).path_history("no/such.txt") == []

    def test_limit_caps_the_list(self, repo: Path) -> None:
        entries = LocalGitEngine.open(str(repo)).path_history(
            "target.txt", limit=1
        )
        assert len(entries) == 1
        assert entries[0].summary == "target v3"


class TestIdleRepackWritesBloomFilters:
    def test_commit_graph_with_changed_paths_appears(self, repo: Path) -> None:
        """유휴 정돈이 경로 히스토리의 가속 장치까지 준비하는가 (ADR-90).

        Bloom 필터의 존재는 BIDX 청크로 확인한다 — "파일이 생겼다"만
        보면 `--changed-paths`가 조용히 빠져도 통과해 버린다.
        """
        engine = LocalGitEngine.open(str(repo))

        assert engine.idle_repack()

        graphs = list((repo / ".git" / "objects" / "info").rglob("*.graph")) + [
            p
            for p in [(repo / ".git" / "objects" / "info" / "commit-graph")]
            if p.exists()
        ]
        assert graphs, "commit-graph 파일이 없다"
        data = b"".join(p.read_bytes() for p in graphs)
        assert b"BIDX" in data, "Bloom 필터(BIDX 청크)가 없다"


class TestDialogEndToEnd:
    def test_history_and_diff_arrive(self, qtbot, repo: Path) -> None:  # noqa: ANN001
        """종단 — 목록이 오고, 첫 커밋의 diff가 그 파일로 좁혀져 온다."""
        from gitclient.ui.path_history_dialog import PathHistoryDialog

        dialog = PathHistoryDialog(str(repo), "target.txt")
        qtbot.addWidget(dialog)

        qtbot.waitUntil(
            lambda: dialog._list.topLevelItemCount() == 3, timeout=TIMEOUT
        )
        assert dialog._list.topLevelItem(0).text(0) == "target v3"

        # 첫 행이 자동 선택돼 diff가 도착한다 — 대상 파일의 변경만.
        qtbot.waitUntil(
            lambda: dialog._diff_model.rowCount() > 0, timeout=TIMEOUT
        )

    def test_no_history_says_so(self, qtbot, repo: Path) -> None:  # noqa: ANN001
        from gitclient.ui.path_history_dialog import PathHistoryDialog

        dialog = PathHistoryDialog(str(repo), "no/such.txt")
        qtbot.addWidget(dialog)

        qtbot.waitUntil(
            lambda: "없습니다" in dialog._status.text(), timeout=TIMEOUT
        )
        assert dialog._list.topLevelItemCount() == 0

    def test_close_with_inflight_loader_is_safe(self, qtbot, repo: Path) -> None:  # noqa: ANN001
        """창이 로더보다 먼저 죽는 경로 — 늦은 배달 3중 방어의 종단."""
        from gitclient.ui.path_history_dialog import PathHistoryDialog

        dialog = PathHistoryDialog(str(repo), "target.txt")
        qtbot.addWidget(dialog)
        dialog.close()  # 로더가 돌고 있는 채로 닫는다

        assert dialog._loaders == {}
        qtbot.wait(200)  # 늦은 배달이 있었다면 여기서 터진다 (pytest-qt 수집)
