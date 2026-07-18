"""패치 합성기 단위 테스트.

부분 스테이징의 어려운 부분은 전부 여기서 고정한다:
줄 번호 재계산, 미선택 삭제줄의 컨텍스트 전환, EOF 개행, 역방향.

순수 파이썬이라 pygit2 없이 검증 가능하고, 실제 적용 가능 여부는
tests/integration/test_hunk_staging.py가 libgit2로 확인한다.
"""

from __future__ import annotations

import pytest

from gitclient.domain.patch import (
    FilePatch,
    PatchError,
    PatchHunk,
    PatchLine,
    format_hunk_header,
    synthesize_patch,
)


def ctx(text: str, old: int, new: int) -> PatchLine:
    return PatchLine(" ", f"{text}\n", old, new)


def add(text: str, new: int) -> PatchLine:
    return PatchLine("+", f"{text}\n", None, new)


def rem(text: str, old: int) -> PatchLine:
    return PatchLine("-", f"{text}\n", old, None)


def simple_patch() -> FilePatch:
    """1..5 중 2와 5를 바꾼 파일. 한 헝크 안에 변경 두 쌍."""
    return FilePatch(
        old_path="a.txt",
        new_path="a.txt",
        hunks=(
            PatchHunk(
                old_start=1,
                old_lines=5,
                new_start=1,
                new_lines=5,
                lines=(
                    ctx("1", 1, 1),
                    rem("2", 2),
                    add("TWO", 2),
                    ctx("3", 3, 3),
                    ctx("4", 4, 4),
                    rem("5", 5),
                    add("FIVE", 5),
                ),
            ),
        ),
    )


def body_lines(patch_text: str) -> list[str]:
    """헤더를 제외한 본문 줄."""
    return [
        line
        for line in patch_text.splitlines()
        if not line.startswith(("diff ", "--- ", "+++ ", "@@", "new file", "deleted file"))
    ]


def headers(patch_text: str) -> list[str]:
    return [line for line in patch_text.splitlines() if line.startswith("@@")]


class TestHeaderFormat:
    def test_single_line_omits_count(self) -> None:
        assert format_hunk_header(2, 1, 2, 1) == "@@ -2 +2 @@"

    def test_multi_line_includes_count(self) -> None:
        assert format_hunk_header(1, 5, 1, 5) == "@@ -1,5 +1,5 @@"

    def test_zero_count(self) -> None:
        assert format_hunk_header(0, 0, 1, 3) == "@@ -0,0 +1,3 @@"


class TestFullHunk:
    def test_selecting_everything_keeps_all_changes(self) -> None:
        text = synthesize_patch(simple_patch())
        assert body_lines(text) == [" 1", "-2", "+TWO", " 3", " 4", "-5", "+FIVE"]

    def test_counts_match_content(self) -> None:
        text = synthesize_patch(simple_patch())
        assert headers(text) == ["@@ -1,5 +1,5 @@"]

    def test_file_headers_are_emitted(self) -> None:
        text = synthesize_patch(simple_patch())
        assert text.startswith("diff --git a/a.txt b/a.txt\n")
        assert "--- a/a.txt\n" in text
        assert "+++ b/a.txt\n" in text


class TestPartialSelection:
    """핵심: 선택하지 않은 삭제줄은 버리지 않고 컨텍스트로 바꿔야 한다."""

    def test_unselected_deletion_becomes_context(self) -> None:
        # 첫 변경 쌍(인덱스 1,2)만 선택. 두 번째 쌍(5,6)은 미선택.
        text = synthesize_patch(simple_patch(), {(0, 1), (0, 2)})
        assert body_lines(text) == [" 1", "-2", "+TWO", " 3", " 4", " 5"]

    def test_unselected_addition_is_dropped(self) -> None:
        text = synthesize_patch(simple_patch(), {(0, 1), (0, 2)})
        assert "+FIVE" not in text

    def test_counts_reflect_partial_selection(self) -> None:
        text = synthesize_patch(simple_patch(), {(0, 1), (0, 2)})
        # old: 1,2,3,4,5 = 5줄 / new: 1,TWO,3,4,5 = 5줄
        assert headers(text) == ["@@ -1,5 +1,5 @@"]

    def test_selecting_only_second_pair(self) -> None:
        text = synthesize_patch(simple_patch(), {(0, 5), (0, 6)})
        assert body_lines(text) == [" 1", " 2", " 3", " 4", "-5", "+FIVE"]

    def test_selecting_only_an_addition(self) -> None:
        """추가만 선택하면 삭제줄은 남는다 — 두 줄이 모두 있는 상태가 된다."""
        text = synthesize_patch(simple_patch(), {(0, 2)})
        assert body_lines(text) == [" 1", " 2", "+TWO", " 3", " 4", " 5"]
        assert headers(text) == ["@@ -1,5 +1,6 @@"]

    def test_selecting_only_a_deletion(self) -> None:
        text = synthesize_patch(simple_patch(), {(0, 1)})
        assert body_lines(text) == [" 1", "-2", " 3", " 4", " 5"]
        assert headers(text) == ["@@ -1,5 +1,4 @@"]

    def test_empty_selection_is_rejected(self) -> None:
        with pytest.raises(PatchError):
            synthesize_patch(simple_patch(), set())


class TestMultipleHunks:
    """두 번째 헝크부터는 앞 헝크의 줄 수 변화만큼 시작 번호가 밀린다."""

    def two_hunk_patch(self) -> FilePatch:
        return FilePatch(
            old_path="m.txt",
            new_path="m.txt",
            hunks=(
                PatchHunk(1, 3, 1, 4, (ctx("a", 1, 1), add("NEW", 2), ctx("b", 2, 3), ctx("c", 3, 4))),
                PatchHunk(20, 3, 21, 3, (ctx("x", 20, 21), rem("y", 21), add("Y", 22), ctx("z", 22, 23))),
            ),
        )

    def test_second_hunk_start_is_shifted(self) -> None:
        text = synthesize_patch(self.two_hunk_patch())
        # 첫 헝크가 한 줄 늘렸으므로 두 번째 헝크의 new_start는 20+1=21
        assert headers(text) == ["@@ -1,3 +1,4 @@", "@@ -20,3 +21,3 @@"]

    def test_hunk_with_no_selection_is_omitted(self) -> None:
        # 두 번째 헝크만 선택 → 첫 헝크는 통째로 빠지고 밀림도 없다
        text = synthesize_patch(self.two_hunk_patch(), {(1, 1), (1, 2)})
        assert headers(text) == ["@@ -20,3 +20,3 @@"]

    def test_only_first_hunk_selected(self) -> None:
        text = synthesize_patch(self.two_hunk_patch(), {(0, 1)})
        assert headers(text) == ["@@ -1,3 +1,4 @@"]
        assert "Y" not in body_lines(text)


class TestReverse:
    """언스테이징은 역방향 패치를 인덱스에 적용해 수행한다.

    원본 diff는 HEAD→인덱스이고, 만들어진 패치는 인덱스에 적용된다.
    따라서 컨텍스트는 **인덱스**(원본의 new 쪽)와 일치해야 한다.
    """

    def test_origins_are_flipped(self) -> None:
        text = synthesize_patch(simple_patch(), {(0, 1), (0, 2)}, reverse=True)
        # 선택된 쌍은 뒤집히고, 미선택 쌍은 인덱스에 있는 쪽(+FIVE)이 컨텍스트로 남는다.
        assert body_lines(text) == [" 1", "+2", "-TWO", " 3", " 4", " FIVE"]

    def test_unselected_deletion_is_dropped_in_reverse(self) -> None:
        """정방향과 정반대 — 미선택 '-'는 인덱스에 없으므로 버려야 한다."""
        text = synthesize_patch(simple_patch(), {(0, 5), (0, 6)}, reverse=True)
        assert "5" not in [line[1:] for line in body_lines(text) if line.startswith(" ")]
        assert body_lines(text) == [" 1", " TWO", " 3", " 4", "+5", "-FIVE"]

    def test_reverse_counts_match_index_side(self) -> None:
        text = synthesize_patch(simple_patch(), {(0, 5), (0, 6)}, reverse=True)
        # 적용 대상(인덱스): 1, TWO, 3, 4, FIVE = 5줄 → 결과도 5줄
        assert headers(text) == ["@@ -1,5 +1,5 @@"]

    def test_reverse_uses_index_side_start(self) -> None:
        """역방향의 기준 줄 번호는 원본의 new_start(인덱스 쪽)다."""
        patch = FilePatch(
            old_path="r.txt",
            new_path="r.txt",
            hunks=(
                PatchHunk(
                    old_start=10,
                    old_lines=2,
                    new_start=30,  # 인덱스에서는 30번째 줄
                    new_lines=2,
                    lines=(ctx("a", 10, 30), rem("b", 11), add("B", 31)),
                ),
            ),
        )
        text = synthesize_patch(patch, reverse=True)
        assert headers(text) == ["@@ -30,2 +30,2 @@"]

    def test_new_file_reverses_to_deleted(self) -> None:
        patch = FilePatch(
            old_path="n.txt",
            new_path="n.txt",
            is_new=True,
            hunks=(PatchHunk(0, 0, 1, 1, (add("only", 1),)),),
        )
        text = synthesize_patch(patch, reverse=True)
        assert "deleted file mode" in text
        assert "+++ /dev/null" in text
        assert body_lines(text) == ["-only"]


class TestNewAndDeletedFiles:
    def test_new_file_headers(self) -> None:
        patch = FilePatch(
            old_path="n.txt",
            new_path="n.txt",
            is_new=True,
            hunks=(PatchHunk(0, 0, 1, 2, (add("a", 1), add("b", 2))),),
        )
        text = synthesize_patch(patch)
        assert "new file mode 100644" in text
        assert "--- /dev/null" in text
        assert headers(text) == ["@@ -0,0 +1,2 @@"]

    def test_partial_new_file(self) -> None:
        """새 파일도 일부 줄만 스테이징할 수 있다."""
        patch = FilePatch(
            old_path="n.txt",
            new_path="n.txt",
            is_new=True,
            hunks=(PatchHunk(0, 0, 1, 3, (add("a", 1), add("b", 2), add("c", 3))),),
        )
        text = synthesize_patch(patch, {(0, 0), (0, 1)})
        assert body_lines(text) == ["+a", "+b"]
        assert headers(text) == ["@@ -0,0 +1,2 @@"]

    def test_deleted_file_headers(self) -> None:
        patch = FilePatch(
            old_path="d.txt",
            new_path="d.txt",
            is_deleted=True,
            hunks=(PatchHunk(1, 2, 0, 0, (rem("a", 1), rem("b", 2))),),
        )
        text = synthesize_patch(patch)
        assert "deleted file mode" in text
        assert "+++ /dev/null" in text
        assert headers(text) == ["@@ -1,2 +0,0 @@"]


class TestEofNewline:
    """EOF 개행 마커는 짝이 되는 줄의 운명을 따라야 한다."""

    def eof_patch(self) -> FilePatch:
        # 마지막 줄에 개행이 없는 파일에서 그 줄을 수정한 경우
        marker = "\n\\ No newline at end of file\n"
        return FilePatch(
            old_path="e.txt",
            new_path="e.txt",
            hunks=(
                PatchHunk(
                    1,
                    2,
                    1,
                    2,
                    (
                        ctx("x", 1, 1),
                        PatchLine("-", "y", 2, None),
                        PatchLine(">", marker, 2, None),
                        PatchLine("+", "Y", None, 2),
                        PatchLine("<", marker, None, 2),
                    ),
                ),
            ),
        )

    def test_markers_follow_kept_lines(self) -> None:
        text = synthesize_patch(self.eof_patch())
        assert text.endswith(
            "-y\n\\ No newline at end of file\n"
            "+Y\n\\ No newline at end of file\n"
        )

    def test_markers_do_not_count_toward_hunk_size(self) -> None:
        text = synthesize_patch(self.eof_patch())
        assert headers(text) == ["@@ -1,2 +1,2 @@"]

    def test_dropped_line_drops_its_marker(self) -> None:
        # 삭제줄만 선택 → 추가줄과 그 마커가 함께 빠진다
        text = synthesize_patch(self.eof_patch(), {(0, 1)})
        assert "+Y" not in text
        assert text.count("No newline") == 1


class TestGuards:
    def test_binary_is_rejected(self) -> None:
        patch = FilePatch("b.bin", "b.bin", is_binary=True)
        with pytest.raises(PatchError) as excinfo:
            synthesize_patch(patch)
        assert "바이너리" in str(excinfo.value)

    def test_no_hunks_is_rejected(self) -> None:
        with pytest.raises(PatchError):
            synthesize_patch(FilePatch("a.txt", "a.txt"))

    def test_unknown_origin_is_rejected(self) -> None:
        patch = FilePatch(
            "a.txt",
            "a.txt",
            hunks=(PatchHunk(1, 1, 1, 1, (PatchLine("?", "weird\n"),)),),
        )
        with pytest.raises(PatchError):
            synthesize_patch(patch)

    def test_context_only_hunk_is_omitted(self) -> None:
        """변경이 없는 헝크만 있으면 적용할 것이 없다."""
        patch = FilePatch(
            "a.txt",
            "a.txt",
            hunks=(PatchHunk(1, 1, 1, 1, (ctx("same", 1, 1),)),),
        )
        with pytest.raises(PatchError):
            synthesize_patch(patch)


class TestContentPreservation:
    """content는 절대 가공하지 않는다 — CRLF나 공백이 바뀌면 패치가 거부된다."""

    def test_crlf_content_is_preserved(self) -> None:
        patch = FilePatch(
            "c.txt",
            "c.txt",
            hunks=(
                PatchHunk(
                    1,
                    1,
                    1,
                    1,
                    (
                        PatchLine("-", "old\r\n", 1, None),
                        PatchLine("+", "new\r\n", None, 1),
                    ),
                ),
            ),
        )
        text = synthesize_patch(patch)
        assert "-old\r\n" in text
        assert "+new\r\n" in text

    def test_trailing_whitespace_is_preserved(self) -> None:
        patch = FilePatch(
            "w.txt",
            "w.txt",
            hunks=(
                PatchHunk(
                    1, 1, 1, 1, (PatchLine("-", "a   \n", 1, None), PatchLine("+", "a\n", None, 1))
                ),
            ),
        )
        text = synthesize_patch(patch)
        assert "-a   \n" in text
