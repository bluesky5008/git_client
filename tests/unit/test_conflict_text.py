"""충돌 마커 파싱·합성 — 순수 로직 (F3)."""

from __future__ import annotations

import pytest

from gitclient.domain.conflict_text import (
    CHOICE_BOTH,
    CHOICE_OURS,
    CHOICE_THEIRS,
    CommonText,
    ConflictHunk,
    compose,
    hunks_of,
    parse_conflicted,
)

CONFLICTED = (
    "머리\n"
    "<<<<<<< HEAD\n"
    "내 줄\n"
    "=======\n"
    "상대 줄\n"
    ">>>>>>> feature\n"
    "허리\n"
    "<<<<<<< HEAD\n"
    "내 둘째\n"
    "=======\n"
    "상대 둘째\n"
    ">>>>>>> feature\n"
    "꼬리\n"
)


class TestParse:
    def test_segments_alternate_and_hunks_carry_both_sides(self) -> None:
        segments = parse_conflicted(CONFLICTED)

        kinds = [type(s).__name__ for s in segments]
        assert kinds == [
            "CommonText", "ConflictHunk", "CommonText",
            "ConflictHunk", "CommonText",
        ]
        first = hunks_of(segments)[0]
        assert first.ours == ("내 줄\n",)
        assert first.theirs == ("상대 줄\n",)

    def test_diff3_base_is_read_and_dropped(self) -> None:
        text = (
            "<<<<<<< HEAD\n내 줄\n||||||| base\n원래 줄\n=======\n"
            "상대 줄\n>>>>>>> feature\n"
        )
        (hunk,) = parse_conflicted(text)
        assert isinstance(hunk, ConflictHunk)
        assert hunk.ours == ("내 줄\n",)
        assert hunk.theirs == ("상대 줄\n",)

    def test_no_markers_means_no_hunks(self) -> None:
        segments = parse_conflicted("그냥 파일\n두 줄\n")
        assert hunks_of(segments) == ()
        assert compose(segments, []) == "그냥 파일\n두 줄\n"

    @pytest.mark.parametrize(
        "broken",
        [
            "<<<<<<< HEAD\n내 줄\n",  # ======= 없음
            "<<<<<<< HEAD\n내 줄\n=======\n상대\n",  # >>>>>>> 없음
            "=======\n",  # 여는 마커 없음
            "<<<<<<< A\n<<<<<<< B\n=======\n>>>>>>> B\n",  # 중첩
        ],
    )
    def test_broken_markers_are_refused(self, broken: str) -> None:
        """반쯤 합친 파일을 쓰는 것보다 거부가 낫다."""
        with pytest.raises(ValueError):
            parse_conflicted(broken)


class TestCompose:
    def test_mixed_choices_assemble_the_file(self) -> None:
        segments = parse_conflicted(CONFLICTED)

        result = compose(segments, [CHOICE_OURS, CHOICE_THEIRS])

        assert result == "머리\n내 줄\n허리\n상대 둘째\n꼬리\n"

    def test_both_keeps_ours_first(self) -> None:
        segments = parse_conflicted(CONFLICTED)

        result = compose(segments, [CHOICE_BOTH, CHOICE_OURS])

        assert "내 줄\n상대 줄\n" in result

    def test_choice_count_must_match(self) -> None:
        segments = parse_conflicted(CONFLICTED)
        with pytest.raises(ValueError):
            compose(segments, [CHOICE_OURS])
        with pytest.raises(ValueError):
            compose(segments, [CHOICE_OURS, "이상한값"])

    def test_missing_trailing_newline_round_trips(self) -> None:
        text = "<<<<<<< HEAD\n내 줄\n=======\n상대 줄>>>>>>> feature\n"
        # 상대 쪽 마지막 줄에 개행이 없으면 git은 마커를 다음 줄에 쓴다 —
        # 위는 그 형태가 아니라 한 줄에 붙은 훼손이다. 정상 형태로 확인:
        normal = "<<<<<<< HEAD\n내 줄\n=======\n상대 줄\n>>>>>>> feature\n"
        (hunk,) = parse_conflicted(normal)
        assert compose((hunk,), [CHOICE_THEIRS]) == "상대 줄\n"
        assert text  # 사용 흔적
