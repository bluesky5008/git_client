"""부분 스테이징 통합 테스트.

단위 테스트(test_patch.py)는 "의도한 패치 텍스트가 나오는가"를 보장한다.
여기서는 **libgit2가 그 패치를 실제로 받아주는가**를 확인한다 — 합성이
문법적으로 맞아도 컨텍스트가 어긋나면 거부되므로, 이 층의 검증이 없으면
기능이 동작한다고 말할 수 없다.
"""

from __future__ import annotations

from pathlib import Path

import pygit2
import pytest

from gitclient.domain.errors import EngineError
from gitclient.infrastructure.local_engine import LocalGitEngine

SIGNATURE = pygit2.Signature("테스터", "tester@example.com", 1700000000, 540)


@pytest.fixture
def repo(tmp_path: Path) -> pygit2.Repository:
    r = pygit2.init_repository(str(tmp_path / "hunk"), initial_head="main")
    r.config["user.name"] = "테스터"
    r.config["user.email"] = "tester@example.com"
    wd = Path(r.workdir)
    (wd / "a.txt").write_text("1\n2\n3\n4\n5\n", encoding="utf-8")
    r.index.add_all()
    r.index.write()
    r.create_commit("HEAD", SIGNATURE, SIGNATURE, "init", r.index.write_tree(), [])
    return r


@pytest.fixture
def engine(repo: pygit2.Repository) -> LocalGitEngine:
    return LocalGitEngine.open(repo.workdir)


def wd(repo: pygit2.Repository) -> Path:
    return Path(repo.workdir)


def fresh_index(repo: pygit2.Repository) -> pygit2.Index:
    """디스크에서 인덱스를 다시 읽는다.

    pygit2의 `repo.index`는 메모리 캐시다. 엔진은 자기 Repository 객체로
    인덱스를 바꾸므로(스레드 안전을 위해 핸들을 분리한다), 테스트가 들고 있는
    객체는 그 변경을 자동으로 보지 못한다. 강제로 다시 읽어야 한다.
    """
    repo.index.read(True)
    return repo.index


def staged_content(repo: pygit2.Repository, path: str) -> str:
    """인덱스에 들어 있는 내용."""
    index = fresh_index(repo)
    return repo[index[path].id].data.decode("utf-8")


def head_content(repo: pygit2.Repository, path: str) -> str:
    return repo[(repo.head.peel(pygit2.Tree) / path).id].data.decode("utf-8")


class TestReadPatch:
    def test_reads_hunks_and_lines(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (wd(repo) / "a.txt").write_text("1\nTWO\n3\n4\n5\n", encoding="utf-8")
        patch = engine.file_patch("a.txt", staged=False)

        assert len(patch.hunks) == 1
        origins = [line.origin for line in patch.hunks[0].lines]
        assert "-" in origins and "+" in origins

    def test_content_keeps_newlines(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        # 표시용과 달리 줄바꿈이 살아 있어야 패치를 만들 수 있다
        (wd(repo) / "a.txt").write_text("1\nTWO\n3\n4\n5\n", encoding="utf-8")
        patch = engine.file_patch("a.txt", staged=False)
        assert all(
            line.content.endswith("\n")
            for line in patch.hunks[0].lines
            if not line.is_eofnl_marker
        )

    def test_untracked_file_is_marked_new(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (wd(repo) / "fresh.txt").write_text("a\nb\n", encoding="utf-8")
        patch = engine.file_patch("fresh.txt", staged=False)
        assert patch.is_new is True

    def test_missing_path_is_actionable(self, engine: LocalGitEngine) -> None:
        with pytest.raises(EngineError) as excinfo:
            engine.file_patch("nope.txt", staged=False)
        assert excinfo.value.action is not None


class TestStagePartial:
    """부분 스테이징의 핵심 계약: 인덱스만 바뀌고 워킹 트리는 그대로."""

    def test_stage_all_changes(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (wd(repo) / "a.txt").write_text("1\nTWO\n3\n4\nFIVE\n", encoding="utf-8")
        engine.stage_partial("a.txt")

        assert staged_content(repo, "a.txt") == "1\nTWO\n3\n4\nFIVE\n"
        assert engine.working_tree_status().unstaged == ()

    def test_stage_only_first_change(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """두 변경 중 첫 번째만 인덱스에 올린다 — 이 기능의 존재 이유."""
        (wd(repo) / "a.txt").write_text("1\nTWO\n3\n4\nFIVE\n", encoding="utf-8")
        patch = engine.file_patch("a.txt", staged=False)

        lines = patch.hunks[0].lines
        first_pair = {
            i for i, line in enumerate(lines) if line.is_change and line.content.strip() in ("2", "TWO")
        }
        engine.stage_partial("a.txt", {(0, i) for i in first_pair})

        # 인덱스: 첫 변경만 반영
        assert staged_content(repo, "a.txt") == "1\nTWO\n3\n4\n5\n"
        # 워킹 트리: 손대지 않았다
        assert (wd(repo) / "a.txt").read_text(encoding="utf-8") == "1\nTWO\n3\n4\nFIVE\n"
        # HEAD: 당연히 그대로
        assert head_content(repo, "a.txt") == "1\n2\n3\n4\n5\n"

    def test_file_appears_in_both_lists_after_partial_stage(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (wd(repo) / "a.txt").write_text("1\nTWO\n3\n4\nFIVE\n", encoding="utf-8")
        patch = engine.file_patch("a.txt", staged=False)
        lines = patch.hunks[0].lines
        first_pair = {
            i for i, line in enumerate(lines) if line.is_change and line.content.strip() in ("2", "TWO")
        }
        engine.stage_partial("a.txt", {(0, i) for i in first_pair})

        status = engine.working_tree_status()
        assert [c.path for c in status.staged] == ["a.txt"]
        assert [c.path for c in status.unstaged] == ["a.txt"]

    def test_stage_partial_new_file(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """새 파일도 일부 줄만 올릴 수 있다."""
        (wd(repo) / "fresh.txt").write_text("a\nb\nc\n", encoding="utf-8")
        patch = engine.file_patch("fresh.txt", staged=False)
        assert len(patch.hunks) == 1

        engine.stage_partial("fresh.txt", {(0, 0), (0, 1)})
        assert staged_content(repo, "fresh.txt") == "a\nb\n"
        assert (wd(repo) / "fresh.txt").read_text(encoding="utf-8") == "a\nb\nc\n"

    def test_empty_selection_is_rejected(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (wd(repo) / "a.txt").write_text("1\nTWO\n3\n4\n5\n", encoding="utf-8")
        with pytest.raises(EngineError):
            engine.stage_partial("a.txt", set())

    def test_binary_partial_is_rejected(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (wd(repo) / "blob.bin").write_bytes(b"\x00\x01\x02" * 64)
        with pytest.raises(EngineError) as excinfo:
            engine.stage_partial("blob.bin")
        assert "바이너리" in excinfo.value.message


class TestUnstagePartial:
    def test_unstage_all(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (wd(repo) / "a.txt").write_text("1\nTWO\n3\n4\nFIVE\n", encoding="utf-8")
        engine.stage_file("a.txt")

        engine.unstage_partial("a.txt")

        assert staged_content(repo, "a.txt") == "1\n2\n3\n4\n5\n"  # HEAD와 동일
        assert (wd(repo) / "a.txt").read_text(encoding="utf-8") == "1\nTWO\n3\n4\nFIVE\n"

    def test_unstage_only_one_change(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """스테이징된 두 변경 중 하나만 내린다."""
        (wd(repo) / "a.txt").write_text("1\nTWO\n3\n4\nFIVE\n", encoding="utf-8")
        engine.stage_file("a.txt")

        patch = engine.file_patch("a.txt", staged=True)
        lines = patch.hunks[0].lines
        second_pair = {
            i
            for i, line in enumerate(lines)
            if line.is_change and line.content.strip() in ("5", "FIVE")
        }
        engine.unstage_partial("a.txt", {(0, i) for i in second_pair})

        # FIVE만 내려가고 TWO는 인덱스에 남는다
        assert staged_content(repo, "a.txt") == "1\nTWO\n3\n4\n5\n"

    def test_roundtrip_stage_then_unstage_is_clean(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (wd(repo) / "a.txt").write_text("1\nTWO\n3\n4\nFIVE\n", encoding="utf-8")
        engine.stage_partial("a.txt")
        engine.unstage_partial("a.txt")

        status = engine.working_tree_status()
        assert status.staged == ()
        assert [c.path for c in status.unstaged] == ["a.txt"]


class TestHardCases:
    def test_multiple_hunks_partial(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """헝크가 둘 이상일 때 두 번째 헝크의 줄 번호가 맞아야 적용된다."""
        big = "\n".join(str(i) for i in range(1, 41)) + "\n"
        (wd(repo) / "big.txt").write_text(big, encoding="utf-8")
        repo.index.add("big.txt")
        repo.index.write()
        repo.create_commit(
            "HEAD", SIGNATURE, SIGNATURE, "big", repo.index.write_tree(),
            [repo.head.target],
        )

        changed = big.replace("2\n", "TWO\n", 1).replace("38\n", "THIRTY8\n", 1)
        (wd(repo) / "big.txt").write_text(changed, encoding="utf-8")

        patch = engine.file_patch("big.txt", staged=False)
        assert len(patch.hunks) == 2, "헝크가 분리되지 않아 이 케이스를 검증할 수 없다"

        # 두 번째 헝크만 스테이징 → 시작 번호 계산이 틀리면 libgit2가 거부한다
        second = {
            (1, i) for i, line in enumerate(patch.hunks[1].lines) if line.is_change
        }
        engine.stage_partial("big.txt", second)

        staged = staged_content(repo, "big.txt")
        assert "THIRTY8" in staged
        assert "TWO" not in staged

    def test_no_trailing_newline(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """EOF 개행이 없는 파일 — 마커를 잘못 다루면 패치가 거부된다."""
        (wd(repo) / "eof.txt").write_text("x\ny", encoding="utf-8")
        repo.index.add("eof.txt")
        repo.index.write()
        repo.create_commit(
            "HEAD", SIGNATURE, SIGNATURE, "eof", repo.index.write_tree(),
            [repo.head.target],
        )

        (wd(repo) / "eof.txt").write_text("x\nY", encoding="utf-8")
        engine.stage_partial("eof.txt")

        assert staged_content(repo, "eof.txt") == "x\nY"

    def test_deleted_file(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (wd(repo) / "a.txt").unlink()
        patch = engine.file_patch("a.txt", staged=False)
        assert patch.is_deleted is True

        engine.stage_partial("a.txt")
        assert "a.txt" not in fresh_index(repo)

    def test_workdir_change_after_read_stages_what_was_seen(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """패치를 읽은 뒤 워킹 트리가 바뀌어도, 사용자가 본 내용이 스테이징된다.

        패치는 인덱스에 적용되고 인덱스 기준으로 검증되므로 워킹 트리 변경은
        적용을 막지 않는다. 이것은 결함이 아니라 의도된 의미론이다 —
        화면에서 고른 변경이 그대로 올라간다 (git add -p와 같다).
        """
        (wd(repo) / "a.txt").write_text("1\nTWO\n3\n4\n5\n", encoding="utf-8")
        patch = engine.file_patch("a.txt", staged=False)
        selected = {
            (0, i) for i, line in enumerate(patch.hunks[0].lines) if line.is_change
        }

        (wd(repo) / "a.txt").write_text("완전히\n다른\n내용\n", encoding="utf-8")
        engine._apply_synthesized(patch, selected, reverse=False, action="스테이징")

        assert staged_content(repo, "a.txt") == "1\nTWO\n3\n4\n5\n"

    def test_stale_index_is_rejected_without_corrupting(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """인덱스가 그 사이 바뀌면 적용이 거부되고 인덱스는 그대로 남아야 한다.

        이것이 실제로 위험한 경합이다. libgit2가 컨텍스트 불일치로 거부하므로
        인덱스가 반쯤 적용된 상태로 오염되지 않는다.
        """
        (wd(repo) / "a.txt").write_text("1\nTWO\n3\n4\n5\n", encoding="utf-8")
        patch = engine.file_patch("a.txt", staged=False)
        selected = {
            (0, i) for i, line in enumerate(patch.hunks[0].lines) if line.is_change
        }

        # 패치를 읽은 뒤 인덱스가 다른 경로로 완전히 바뀐다
        (wd(repo) / "a.txt").write_text("전혀\n다른\n인덱스\n", encoding="utf-8")
        engine.stage_file("a.txt")
        before = staged_content(repo, "a.txt")

        with pytest.raises(EngineError) as excinfo:
            engine._apply_synthesized(
                patch, selected, reverse=False, action="스테이징"
            )
        assert excinfo.value.action is not None
        assert staged_content(repo, "a.txt") == before  # 오염 없음

    def test_partial_stage_of_deleted_file_keeps_the_rest(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """삭제된 파일을 부분 스테이징해도 미선택 줄은 인덱스에 남아야 한다.

        확정된 치명 결함의 회귀 테스트: `deleted file mode` 헤더를 그대로 내면
        libgit2가 본문보다 헤더를 우선해 **인덱스 엔트리를 통째로 지운다**.
        예외가 나지 않으므로 "성공했는가"가 아니라 "무엇이 남았는가"를 봐야 잡힌다.
        """
        (wd(repo) / "a.txt").unlink()
        patch = engine.file_patch("a.txt", staged=False)
        assert patch.is_deleted is True

        # 5줄 중 앞 2줄만 선택해 삭제를 스테이징
        first_two = {(0, 0), (0, 1)}
        engine.stage_partial("a.txt", first_two)

        assert staged_content(repo, "a.txt") == "3\n4\n5\n"

    def test_full_stage_of_deleted_file_still_deletes(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """전체 선택일 때는 삭제 헤더가 유지되어야 한다 (위 수정의 역방향 보호)."""
        (wd(repo) / "a.txt").unlink()
        engine.stage_partial("a.txt")
        assert "a.txt" not in fresh_index(repo)

    def test_partial_unstage_of_new_file_keeps_the_rest(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """새로 스테이징한 파일을 부분 언스테이징해도 나머지는 남아야 한다.

        같은 결함의 reverse 경로 — is_new가 is_deleted로 뒤집히며 발생한다.
        """
        (wd(repo) / "n.txt").write_text("1\n2\n3\n4\n", encoding="utf-8")
        engine.stage_file("n.txt")

        engine.unstage_partial("n.txt", {(0, 0), (0, 1)})

        assert staged_content(repo, "n.txt") == "3\n4\n"

    def test_partial_stage_preserves_executable_bit(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """실행 비트가 있는 파일을 부분 스테이징해도 모드가 유지되어야 한다."""
        script = wd(repo) / "s.sh"
        script.write_text("#!/bin/sh\necho one\necho two\n", encoding="utf-8")
        repo.index.add("s.sh")
        entry = repo.index["s.sh"]
        repo.index.remove("s.sh")
        repo.index.add(pygit2.IndexEntry("s.sh", entry.id, pygit2.enums.FileMode.BLOB_EXECUTABLE))
        repo.index.write()
        repo.create_commit(
            "HEAD", SIGNATURE, SIGNATURE, "script", repo.index.write_tree(),
            [repo.head.target],
        )
        assert fresh_index(repo)["s.sh"].mode == pygit2.enums.FileMode.BLOB_EXECUTABLE

        script.write_text("#!/bin/sh\necho ONE\necho two\n", encoding="utf-8")
        engine.stage_partial("s.sh")

        assert fresh_index(repo)["s.sh"].mode == pygit2.enums.FileMode.BLOB_EXECUTABLE

    def test_partial_stage_with_missing_eof_newline_context(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """EOF 개행 없는 삭제줄이 컨텍스트로 바뀔 때 마커가 따라가야 한다.

        개행 없는 컨텍스트 줄에 `\\ No newline at end of file`이 붙지 않으면
        libgit2가 `invalid patch instruction`으로 거부한다(실측 확인).
        """
        target = wd(repo) / "e.txt"
        target.write_bytes(b"a\nb\nc")  # 마지막 줄 개행 없음
        repo.index.add("e.txt")
        repo.index.write()
        repo.create_commit(
            "HEAD", SIGNATURE, SIGNATURE, "eof2", repo.index.write_tree(),
            [repo.head.target],
        )

        target.write_bytes(b"a\nB\nc\n")  # b→B 수정 + 마지막 줄에 개행 추가
        patch = engine.file_patch("e.txt", staged=False)
        lines = patch.hunks[0].lines

        # b, c가 한 묶음으로 그룹화된다 (-b -c +B +c). 여기서 b→B만 고르면
        # 컨텍스트가 된 c가 +B보다 앞에 놓여 내용 순서가 뒤바뀐다.
        # 조용히 손상시키는 대신 거부해야 한다.
        selected = {
            (0, i)
            for i, line in enumerate(lines)
            if line.is_change and line.content.rstrip("\n") in ("b", "B")
        }
        assert len(selected) == 2, "선택 대상을 잘못 골랐다"

        with pytest.raises(EngineError) as excinfo:
            engine.stage_partial("e.txt", selected)
        assert "헝크 전체" in excinfo.value.message

        # 거부됐으므로 인덱스는 그대로다 (손상 없음)
        assert staged_content(repo, "e.txt") == "a\nb\nc"

    def test_whole_hunk_with_eof_change_still_works(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """애매한 묶음도 헝크 전체 선택이면 안전하게 적용된다."""
        target = wd(repo) / "e2.txt"
        target.write_bytes(b"a\nb\nc")
        repo.index.add("e2.txt")
        repo.index.write()
        repo.create_commit(
            "HEAD", SIGNATURE, SIGNATURE, "eof3", repo.index.write_tree(),
            [repo.head.target],
        )

        target.write_bytes(b"a\nB\nc\n")
        engine.stage_partial("e2.txt")  # 전체 선택

        assert staged_content(repo, "e2.txt") == "a\nB\nc\n"

    def test_rename_partial_is_rejected_not_half_applied(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        """이름 변경은 부분 스테이징을 거부해야 한다.

        확정된 결함의 회귀 테스트: 예전에는 서로 다른 a/ b/ 경로를 내보내
        원래 경로가 인덱스에 남고 두 경로가 동시에 스테이징됐다. 경로를
        하나로 고정하면 중복은 사라지지만 이번엔 이름 변경이 절반만 적용된다.
        반쯤 적용하는 대신 거부하고 파일 단위 조작으로 안내한다.
        """
        source = wd(repo) / "a.txt"
        renamed = wd(repo) / "renamed.txt"
        renamed.write_text("1\nTWO\n3\n4\n5\n", encoding="utf-8")
        source.unlink()

        engine.stage_file("a.txt")
        engine.stage_file("renamed.txt")

        index_before = sorted(e.path for e in fresh_index(repo))
        assert index_before == ["renamed.txt"]

        patch = engine.file_patch("renamed.txt", staged=True)
        if not patch.is_rename:
            pytest.skip("이 조합에서는 libgit2가 이름 변경으로 묶지 않았다")

        assert patch.can_stage_partially is False
        with pytest.raises(EngineError) as excinfo:
            engine.unstage_partial("renamed.txt")
        assert "이름이 바뀐" in excinfo.value.message

        # 인덱스는 손대지 않았다
        assert sorted(e.path for e in fresh_index(repo)) == index_before

    def test_crlf_file(
        self, repo: pygit2.Repository, engine: LocalGitEngine
    ) -> None:
        (wd(repo) / "crlf.txt").write_bytes(b"p\r\nq\r\n")
        repo.index.add("crlf.txt")
        repo.index.write()
        repo.create_commit(
            "HEAD", SIGNATURE, SIGNATURE, "crlf", repo.index.write_tree(),
            [repo.head.target],
        )

        (wd(repo) / "crlf.txt").write_bytes(b"p\r\nQ\r\n")
        engine.stage_partial("crlf.txt")

        assert "Q" in staged_content(repo, "crlf.txt")
