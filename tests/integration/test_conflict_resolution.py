"""충돌 해결 (Phase 4 증분 2).

**마커 없는 충돌이 이 증분의 존재 이유다.** 바이너리 충돌과 삭제 계열
충돌은 워킹 트리에 마커가 들어가지 않아서, 지금까지 안내하던 "편집기로
마커를 정리하고 스테이징하라"가 아예 통하지 않았다 — 이 앱에서 해결할
방법이 없던 유일한 상태다 (design.md §13-3).

여기서 검증하는 것:
  1. 양쪽 내용을 인덱스 스테이지에서 복원하는가 (워킹 트리로는 불가능하다)
  2. 종류를 가리지 않고 한쪽 선택으로 해결되는가 — 특히 마커 없는 것들
  3. 해결한 결과가 커밋 가능한 상태인가
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gitclient.domain.errors import EngineError
from gitclient.domain.models import ConflictChoice, ConflictSide
from gitclient.infrastructure.local_engine import LocalGitEngine
from tests.integration.remote_harness import AUTHOR_ENV, git


def commit_all(repo: Path, message: str) -> None:
    git("add", "-A", cwd=repo)
    git(*AUTHOR_ENV, "commit", "--quiet", "-m", message, cwd=repo)


def make_repo(tmp_path: Path, kind: str) -> Path:
    """종류별 충돌을 만든다. 전부 실제 git으로 만든 상태다."""
    root = tmp_path / "work"
    root.mkdir()
    git("init", "--quiet", "-b", "main", str(root))
    if kind == "binary":
        (root / "f.bin").write_bytes(bytes(range(256)) * 4)
    else:
        (root / "f.txt").write_text("base\n", encoding="utf-8")
    commit_all(root, "base")

    git("checkout", "--quiet", "-b", "feature", cwd=root)
    if kind == "binary":
        (root / "f.bin").write_bytes(bytes(range(255, -1, -1)) * 4)
    elif kind == "deleted_by_them":
        (root / "f.txt").unlink()
    elif kind == "both_added":
        (root / "new.txt").write_text("theirs\n", encoding="utf-8")
    else:
        (root / "f.txt").write_text("theirs\n", encoding="utf-8")
    commit_all(root, "feature")

    git("checkout", "--quiet", "main", cwd=root)
    if kind == "binary":
        (root / "f.bin").write_bytes(bytes(range(128)) * 8)
    elif kind == "deleted_by_them":
        (root / "f.txt").write_text("ours\n", encoding="utf-8")
    elif kind == "both_added":
        (root / "new.txt").write_text("ours\n", encoding="utf-8")
    else:
        (root / "f.txt").write_text("ours\n", encoding="utf-8")
    commit_all(root, "main")
    return root


def start_merge(root: Path) -> LocalGitEngine:
    engine = LocalGitEngine.open(str(root))
    engine.merge("refs/heads/feature")
    return engine


class TestBothSidesAreRecoverable:
    """워킹 트리로는 복원할 수 없다 — 인덱스 스테이지에서 읽어야 한다."""

    def test_text_conflict_exposes_both_sides(self, tmp_path: Path) -> None:
        engine = start_merge(make_repo(tmp_path, "both_modified"))

        detail = engine.conflict_detail("f.txt")

        # 줄 끝은 플랫폼이 정한다 — 엔진은 인덱스에 있는 것을 그대로 준다.
        assert detail.ours.text.strip() == "ours"
        assert detail.theirs.text.strip() == "theirs"
        assert detail.can_show_text

    def test_binary_conflict_is_marked_unviewable(self, tmp_path: Path) -> None:
        """줄 개념이 없어 나란히 볼 수 없다 — 그래도 선택은 가능해야 한다."""
        engine = start_merge(make_repo(tmp_path, "binary"))

        detail = engine.conflict_detail("f.bin")

        assert detail.is_binary
        assert not detail.can_show_text
        assert detail.ours.exists and detail.theirs.exists

    def test_deleted_side_is_absent_not_empty(self, tmp_path: Path) -> None:
        """'없음'과 '빈 파일'은 다르다 — 화면이 그것을 구분해 그려야 한다."""
        engine = start_merge(make_repo(tmp_path, "deleted_by_them"))

        detail = engine.conflict_detail("f.txt")

        assert detail.side is ConflictSide.DELETED_BY_THEM
        assert detail.ours.exists
        assert not detail.theirs.exists

    def test_unknown_path_is_actionable(self, tmp_path: Path) -> None:
        engine = start_merge(make_repo(tmp_path, "both_modified"))

        with pytest.raises(EngineError) as excinfo:
            engine.conflict_detail("nope.txt")

        assert excinfo.value.action is not None


class TestResolveByChoosingASide:
    """종류를 가리지 않고 통해야 한다."""

    @pytest.mark.parametrize("kind,path", [
        ("both_modified", "f.txt"),
        ("binary", "f.bin"),
        ("both_added", "new.txt"),
    ])
    def test_taking_ours_clears_the_conflict(
        self, tmp_path: Path, kind: str, path: str
    ) -> None:
        engine = start_merge(make_repo(tmp_path, kind))
        expected = engine.conflict_detail(path).ours.data

        engine.resolve_conflict(path, ConflictChoice.OURS)

        assert engine.merge_conflicts() == ()
        assert (tmp_path / "work" / path).read_bytes() == expected

    @pytest.mark.parametrize("kind,path", [
        ("both_modified", "f.txt"),
        ("binary", "f.bin"),
        ("both_added", "new.txt"),
    ])
    def test_taking_theirs_clears_the_conflict(
        self, tmp_path: Path, kind: str, path: str
    ) -> None:
        engine = start_merge(make_repo(tmp_path, kind))
        expected = engine.conflict_detail(path).theirs.data

        engine.resolve_conflict(path, ConflictChoice.THEIRS)

        assert engine.merge_conflicts() == ()
        assert (tmp_path / "work" / path).read_bytes() == expected

    def test_choosing_the_deleted_side_removes_the_file(
        self, tmp_path: Path
    ) -> None:
        """가장 까다로운 경우 — 고른 쪽에 내용이 아예 없다.

        `index.remove`를 부르면 "stage 0에 없다"로 실패한다(실측). 충돌
        항목만 지우면 인덱스에서 경로가 사라져 git이 삭제로 읽는다.
        """
        root = make_repo(tmp_path, "deleted_by_them")
        engine = start_merge(root)

        engine.resolve_conflict("f.txt", ConflictChoice.THEIRS)

        assert engine.merge_conflicts() == ()
        assert not (root / "f.txt").exists()
        assert "D" in git("status", "--porcelain", cwd=root).stdout

    def test_keeping_our_side_of_a_deletion(self, tmp_path: Path) -> None:
        root = make_repo(tmp_path, "deleted_by_them")
        engine = start_merge(root)

        engine.resolve_conflict("f.txt", ConflictChoice.OURS)

        assert engine.merge_conflicts() == ()
        assert (root / "f.txt").read_text(encoding="utf-8") == "ours\n"

    def test_resolving_a_non_conflict_is_actionable(
        self, tmp_path: Path
    ) -> None:
        engine = start_merge(make_repo(tmp_path, "both_modified"))
        engine.resolve_conflict("f.txt", ConflictChoice.OURS)

        with pytest.raises(EngineError) as excinfo:
            engine.resolve_conflict("f.txt", ConflictChoice.OURS)

        assert excinfo.value.action is not None


class TestResolvedMergeCanBeCommitted:
    """해결이 끝이 아니다 — 커밋까지 이어져야 병합이 완료된다."""

    def test_binary_conflict_resolution_completes_the_merge(
        self, tmp_path: Path
    ) -> None:
        """지금까지 앱 안에서 끝낼 수 없던 경로다."""
        root = make_repo(tmp_path, "binary")
        engine = start_merge(root)

        engine.resolve_conflict("f.bin", ConflictChoice.THEIRS)
        engine.create_commit("바이너리 충돌 해결")

        parents = git("rev-list", "--parents", "-n", "1", "HEAD", cwd=root).stdout
        assert len(parents.split()) == 3, "머지 커밋이 아니다"
        assert not (root / ".git" / "MERGE_HEAD").exists()

    def test_delete_conflict_resolution_completes_the_merge(
        self, tmp_path: Path
    ) -> None:
        root = make_repo(tmp_path, "deleted_by_them")
        engine = start_merge(root)

        engine.resolve_conflict("f.txt", ConflictChoice.THEIRS)
        engine.create_commit("삭제 충돌 해결")

        assert not (root / ".git" / "MERGE_HEAD").exists()
        assert not (root / "f.txt").exists()


class TestConflictPanelIsPersistent:
    """**상태는 저장소에 있는데 안내는 메모리에만 있었다** (§13-3).

    이전에는 병합이 충돌로 끝나는 순간 모달을 한 번 띄우고 말았다. 앱을
    다시 켜면 저장소는 여전히 병합 중인데 화면에 아무 흔적이 없었다.
    """

    TIMEOUT = 60_000

    def _window(self, qtbot, root: Path):  # noqa: ANN001, ANN202
        from gitclient.ui.main_window import MainWindow

        w = MainWindow()
        qtbot.addWidget(w)
        w._report = lambda _e: None
        w._notify = lambda *a, **k: None
        w.open_repository(str(root))
        qtbot.waitUntil(lambda: not w._loading, timeout=self.TIMEOUT)
        return w

    def test_panel_is_hidden_without_conflicts(self, qtbot, tmp_path: Path) -> None:  # noqa: ANN001
        """평소에 자리를 차지하면 대부분의 시간 동안 쓸모없는 공간이 된다."""
        window = self._window(qtbot, make_repo(tmp_path, "both_modified"))

        assert window._conflict_box.isHidden()

    def test_panel_appears_for_a_repository_already_in_conflict(
        self, qtbot, tmp_path: Path  # noqa: ANN001
    ) -> None:
        """저장소를 **여는 것만으로** 충돌이 드러나야 한다 — 모달이 아니라."""
        root = make_repo(tmp_path, "binary")
        start_merge(root)

        window = self._window(qtbot, root)

        assert not window._conflict_box.isHidden()
        assert [c.path for c in window._merge_conflicts] == ["f.bin"]

    def test_resolving_through_the_panel_clears_the_conflict(
        self, qtbot, tmp_path: Path  # noqa: ANN001
    ) -> None:
        """마커 없는 충돌을 **앱 안에서** 끝낼 수 있는지 — 이 증분의 목적."""
        root = make_repo(tmp_path, "binary")
        start_merge(root)
        window = self._window(qtbot, root)

        window._on_resolve_conflict("f.bin", ConflictChoice.THEIRS)
        qtbot.waitUntil(
            lambda: window._write_queue is not None
            and not window._write_queue.is_busy,
            timeout=self.TIMEOUT,
        )
        qtbot.waitUntil(lambda: not window._loading, timeout=self.TIMEOUT)

        assert LocalGitEngine.open(str(root)).merge_conflicts() == ()
        assert window._conflict_box.isHidden(), "다 해결했는데 패널이 남았다"

    def test_panel_shows_both_sides(self, qtbot, tmp_path: Path) -> None:  # noqa: ANN001
        root = make_repo(tmp_path, "both_modified")
        start_merge(root)
        window = self._window(qtbot, root)

        window._on_conflict_selected("f.txt")

        assert "ours" in window._conflict_panel._ours.toPlainText()
        assert "theirs" in window._conflict_panel._theirs.toPlainText()

    def test_deleted_side_is_explained_not_shown_empty(
        self, qtbot, tmp_path: Path  # noqa: ANN001
    ) -> None:
        """'없음'을 빈 칸으로 그리면 '빈 파일'과 구분되지 않는다."""
        root = make_repo(tmp_path, "deleted_by_them")
        start_merge(root)
        window = self._window(qtbot, root)

        window._on_conflict_selected("f.txt")

        assert "지워졌습니다" in window._conflict_panel._theirs.toPlainText()

    def test_binary_conflict_still_offers_a_choice(
        self, qtbot, tmp_path: Path  # noqa: ANN001
    ) -> None:
        """내용을 못 봐도 고를 수는 있어야 한다 — 아니면 해결할 길이 없다."""
        root = make_repo(tmp_path, "binary")
        start_merge(root)
        window = self._window(qtbot, root)

        window._on_conflict_selected("f.bin")

        assert window._conflict_panel._take_ours.isEnabled()
        assert window._conflict_panel._take_theirs.isEnabled()
        assert "바이너리" in window._conflict_panel._hint.text()


class TestConflictsFromOtherSources:
    """충돌은 병합에서만 생기지 않는다 (§13-2).

    실측: stash pop 충돌은 `state()`가 NONE인데 인덱스에는 충돌이 있다.
    상태로 걸러내면 그 충돌이 화면에서 통째로 사라져, 사용자는 아무 안내
    없이 마커가 든 파일을 마주하게 된다.
    """

    def _stash_conflict(self, tmp_path: Path) -> Path:
        import os
        import subprocess

        root = tmp_path / "work"
        root.mkdir()
        git("init", "--quiet", "-b", "main", str(root))
        (root / "f.txt").write_text("base\n", encoding="utf-8")
        commit_all(root, "base")
        (root / "f.txt").write_text("stashed\n", encoding="utf-8")
        git("stash", "push", "-m", "w", cwd=root)
        (root / "f.txt").write_text("other\n", encoding="utf-8")
        commit_all(root, "conflicting")
        subprocess.run(
            ["git", "stash", "pop"], cwd=root,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": os.devnull},
            capture_output=True,
        )
        return root

    def test_stash_conflicts_are_listed(self, tmp_path: Path) -> None:
        engine = LocalGitEngine.open(str(self._stash_conflict(tmp_path)))

        assert [c.path for c in engine.index_conflicts()] == ["f.txt"]
        assert not engine.is_merging(), "병합이 아닌데 병합이라고 한다"

    def test_stash_conflicts_are_resolvable(self, tmp_path: Path) -> None:
        """해결 경로도 출처를 가리지 않아야 한다."""
        root = self._stash_conflict(tmp_path)
        engine = LocalGitEngine.open(str(root))

        engine.resolve_conflict("f.txt", ConflictChoice.OURS)

        assert engine.index_conflicts() == ()

    def test_merge_specific_view_still_excludes_them(
        self, tmp_path: Path
    ) -> None:
        """'병합 중단'처럼 병합에만 의미가 있는 기능은 계속 걸러야 한다.

        stash 충돌을 병합으로 오인하면 중단이 엉뚱한 것을 되돌린다.
        """
        engine = LocalGitEngine.open(str(self._stash_conflict(tmp_path)))

        assert engine.merge_conflicts() == ()


class TestRebaseConflictsAreShownWithHonestLabels:
    """**감추는 것은 해결이 아니었다** (ADR-65 → 증분 3에서 해제).

    증분 2는 rebase 충돌을 목록에서 뺐다. 스테이지 2/3의 주체가 병합과
    반대라 "내 것 사용"이 사용자 자신의 커밋을 버렸고(실측: `rebase
    --continue`가 성공하며 소실), 앱에 `--continue`가 없어 라벨을 고쳐도
    마무리를 못 했기 때문이다.

    그런데 감춘 결과 사용자는 **아무 안내 없이 마커가 든 파일을 마주했다.**
    증분 3에서 두 이유가 모두 해소되어 제한을 푼다. 이 클래스가 지키는 것은
    "노출한다"가 아니라 **"노출하되 이름이 내용과 맞는다"**이다 — 그 짝이
    깨지면 옛 손실이 그대로 돌아온다.
    """

    def _rebase_conflict(self, tmp_path: Path) -> Path:
        import os
        import subprocess

        root = tmp_path / "work"
        root.mkdir()
        git("init", "--quiet", "-b", "main", str(root))
        (root / "f.txt").write_text("base\n", encoding="utf-8")
        commit_all(root, "base")
        git("checkout", "--quiet", "-b", "topic", cwd=root)
        (root / "f.txt").write_text("MY OWN WORK\n", encoding="utf-8")
        commit_all(root, "topic")
        git("checkout", "--quiet", "main", cwd=root)
        (root / "f.txt").write_text("UPSTREAM\n", encoding="utf-8")
        commit_all(root, "main")
        git("checkout", "--quiet", "topic", cwd=root)
        subprocess.run(
            ["git", "rebase", "main"], cwd=root,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                 "GIT_CONFIG_GLOBAL": os.devnull},
            capture_output=True,
        )
        return root

    def test_rebase_conflicts_are_listed(self, tmp_path: Path) -> None:
        engine = LocalGitEngine.open(str(self._rebase_conflict(tmp_path)))

        assert [c.path for c in engine.index_conflicts()] == ["f.txt"]

    def test_the_side_called_mine_holds_my_commit(self, tmp_path: Path) -> None:
        """**이 한 줄이 옛 손실을 막는다.**

        화면이 "내 커밋"이라 부르는 쪽에 정말 사용자의 내용이 있어야 한다.
        라벨과 내용을 함께 확인하지 않으면 노출을 되살린 것이 곧 결함을
        되살린 것이 된다.
        """
        root = self._rebase_conflict(tmp_path)
        engine = LocalGitEngine.open(str(root))

        state = engine.operation_state()
        detail = engine.conflict_detail("f.txt")

        assert "내 커밋" in state.labels.theirs
        assert detail.theirs.text.strip() == "MY OWN WORK"
        assert "upstream" in state.labels.ours
        assert detail.ours.text.strip() == "UPSTREAM"

    def test_merge_specific_view_still_excludes_a_rebase(
        self, tmp_path: Path
    ) -> None:
        """'병합 중단'이 rebase를 되돌리면 시퀀서가 파괴된다."""
        engine = LocalGitEngine.open(str(self._rebase_conflict(tmp_path)))

        assert engine.merge_conflicts() == ()
        assert not engine.is_merging()


class TestSpecialFileTypesAreRefused:
    """조용히 망가뜨리느니 거부하고 무엇을 해야 하는지 알린다.

    가드를 직접 검증한다 — 심볼릭 링크·서브모듈 충돌을 Windows에서
    합성하려면 워킹 트리를 인덱스와 어긋나게 만들어야 해서, 정작 검증하려는
    코드에 닿기 전에 병합 가드에 걸린다.
    """

    class _Entry:
        def __init__(self, mode: int) -> None:
            self.mode = mode

    def test_symlink_is_refused(self) -> None:
        """blob 내용이 링크 **대상 경로**라, 그대로 쓰면 링크가 아니라 파일이 된다."""
        with pytest.raises(EngineError) as excinfo:
            LocalGitEngine._require_plain_file("link", self._Entry(0o120000))

        assert excinfo.value.action is not None
        assert "심볼릭 링크" in excinfo.value.message

    def test_submodule_is_refused(self) -> None:
        """gitlink의 id는 blob이 아니라 **커밋**이라 `.data`를 읽는 것부터 틀렸다."""
        with pytest.raises(EngineError) as excinfo:
            LocalGitEngine._require_plain_file("sub", self._Entry(0o160000))

        assert excinfo.value.action is not None
        assert "서브모듈" in excinfo.value.message

    def test_plain_files_pass(self) -> None:
        LocalGitEngine._require_plain_file("f.txt", self._Entry(0o100644))
        LocalGitEngine._require_plain_file("run.sh", self._Entry(0o100755))


class TestExecutableBitSurvives:
    def test_mode_is_preserved(self, tmp_path: Path) -> None:
        """Windows에는 실행 비트가 없어 `index.add`가 100755를 100644로 떨군다.

        스크립트가 조용히 실행 불가가 되고, 그 사실은 커밋 뒤 다른 사람의
        CI에서야 드러난다.
        """
        root = tmp_path / "work"
        root.mkdir()
        git("init", "--quiet", "-b", "main", str(root))
        (root / "run.sh").write_text("base\n", encoding="utf-8")
        commit_all(root, "base")
        git("update-index", "--chmod=+x", "run.sh", cwd=root)
        commit_all(root, "exec")
        git("checkout", "--quiet", "-b", "feature", cwd=root)
        (root / "run.sh").write_text("theirs\n", encoding="utf-8")
        commit_all(root, "feature")
        git("checkout", "--quiet", "main", cwd=root)
        (root / "run.sh").write_text("ours\n", encoding="utf-8")
        commit_all(root, "main")
        engine = LocalGitEngine.open(str(root))
        engine.merge("refs/heads/feature")

        engine.resolve_conflict("run.sh", ConflictChoice.THEIRS)

        staged = git("ls-files", "--stage", "run.sh", cwd=root).stdout
        assert staged.startswith("100755"), f"실행 비트가 사라졌다: {staged[:20]}"
