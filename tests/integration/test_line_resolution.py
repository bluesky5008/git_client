"""줄 단위로 골라 합치기 — 편집기 없이 양쪽에서 일부씩 (F3, AC-03)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gitclient.domain.conflict_text import CHOICE_OURS, CHOICE_THEIRS
from gitclient.domain.errors import EngineError
from gitclient.infrastructure.local_engine import LocalGitEngine
from tests.integration.remote_harness import AUTHOR_ENV, git

TIMEOUT = 15_000
BLOCK = ("공통\n" * 8)  # 구획이 하나로 합쳐지지 않게 충분히 띄운다


def commit_all(repo: Path, message: str) -> None:
    git("add", "-A", cwd=repo)
    git(*AUTHOR_ENV, "commit", "--quiet", "-m", message, cwd=repo)


def write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)


@pytest.fixture
def two_hunk_conflict(tmp_path: Path) -> Path:
    """서로 떨어진 두 영역을 양쪽이 다르게 고친 저장소 — 구획 2개."""
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    write(root / "f.txt", f"첫-기준\n{BLOCK}둘-기준\n")
    commit_all(root, "base")

    git("checkout", "--quiet", "-b", "feature", cwd=root)
    write(root / "f.txt", f"첫-상대\n{BLOCK}둘-상대\n")
    commit_all(root, "feature")

    git("checkout", "--quiet", "main", cwd=root)
    write(root / "f.txt", f"첫-내것\n{BLOCK}둘-내것\n")
    commit_all(root, "main")

    result = subprocess.run(
        ["git", "merge", "feature"], cwd=root, capture_output=True
    )
    assert result.returncode != 0, "충돌이 나야 하는 픽스처다"
    return root


class TestEngineLineResolution:
    def test_mixed_choices_resolve_and_stage(self, two_hunk_conflict: Path) -> None:
        """AC-03의 본체 — 구획 A는 내 것, B는 상대 것."""
        engine = LocalGitEngine.open(str(two_hunk_conflict))
        assert len(engine.index_conflicts()) == 1

        engine.resolve_conflict_lines("f.txt", [CHOICE_OURS, CHOICE_THEIRS])

        content = (two_hunk_conflict / "f.txt").read_text(encoding="utf-8")
        assert content == f"첫-내것\n{BLOCK}둘-상대\n"
        assert engine.index_conflicts() == (), "충돌이 풀리지 않았다"
        staged = git(
            "ls-files", "--stage", "f.txt", cwd=two_hunk_conflict
        ).stdout
        assert staged.startswith("100644"), "스테이징되지 않았다"

    def test_edited_markers_are_refused_with_the_editor_path(
        self, two_hunk_conflict: Path
    ) -> None:
        """마커를 손댔으면 반쯤 합치지 않는다 — 편집기 경로를 안내한다."""
        target = two_hunk_conflict / "f.txt"
        target.write_text(
            target.read_text(encoding="utf-8").replace("=======\n", "", 1),
            encoding="utf-8",
        )
        engine = LocalGitEngine.open(str(two_hunk_conflict))

        with pytest.raises(EngineError) as caught:
            engine.resolve_conflict_lines("f.txt", [CHOICE_OURS, CHOICE_THEIRS])

        assert "편집기" in (caught.value.action or "")
        assert len(engine.index_conflicts()) == 1, "실패했는데 충돌이 지워졌다"

    def test_choice_count_mismatch_is_refused(self, two_hunk_conflict: Path) -> None:
        engine = LocalGitEngine.open(str(two_hunk_conflict))
        with pytest.raises(EngineError):
            engine.resolve_conflict_lines("f.txt", [CHOICE_OURS])


class TestDialogAndPanel:
    def test_dialog_emits_the_picked_choices(self, qtbot) -> None:  # noqa: ANN001
        from gitclient.domain.conflict_text import ConflictHunk
        from gitclient.domain.models import RepoOperation, conflict_labels
        from gitclient.ui.conflict_lines_dialog import ConflictLinesDialog

        hunks = (
            ConflictHunk(("내1\n",), ("상1\n",)),
            ConflictHunk(("내2\n",), ("상2\n",)),
        )
        dialog = ConflictLinesDialog(
            "f.txt", hunks, conflict_labels(RepoOperation.MERGE)
        )
        qtbot.addWidget(dialog)
        # 구획 2: '상대 것' 라디오(두 번째 버튼)를 고른다.
        dialog._groups[1].buttons()[1].setChecked(True)

        assert dialog.choices() == [CHOICE_OURS, CHOICE_THEIRS]

    def test_panel_button_enables_only_for_text_pairs(
        self, qtbot, two_hunk_conflict: Path  # noqa: ANN001
    ) -> None:
        from gitclient.ui.conflict_panel import ConflictPanel

        engine = LocalGitEngine.open(str(two_hunk_conflict))
        panel = ConflictPanel()
        qtbot.addWidget(panel)
        panel.set_conflicts(engine.index_conflicts())
        panel._current_path = "f.txt"

        panel.show_detail(engine.conflict_detail("f.txt"))

        assert panel._pick_lines.isEnabled(), "텍스트 충돌인데 잠겨 있다"

    def test_window_end_to_end(self, qtbot, two_hunk_conflict: Path) -> None:  # noqa: ANN001
        """창 경유 — 제출이 큐를 지나 파일이 조립되고 충돌이 풀린다."""
        from gitclient.ui.main_window import MainWindow

        window = MainWindow()
        qtbot.addWidget(window)
        window._report = lambda _e: None
        window.open_repository(str(two_hunk_conflict))
        qtbot.waitUntil(lambda: not window._loading, timeout=TIMEOUT)

        window._submit_write(
            "충돌 해결(줄 단위): f.txt",
            lambda engine: engine.resolve_conflict_lines(
                "f.txt", [CHOICE_THEIRS, CHOICE_OURS]
            ),
        )
        qtbot.waitUntil(
            lambda: not window._write_queue.is_busy, timeout=TIMEOUT
        )

        content = (two_hunk_conflict / "f.txt").read_text(encoding="utf-8")
        assert content == f"첫-상대\n{BLOCK}둘-내것\n"
        assert window._engine.index_conflicts() == ()
