"""충돌 마커 파일의 구획 파싱과 합성 (F3, backlog §2.2).

**워킹 트리의 마커 파일을 파싱한다** — git이 이미 3-way 병합을 끝내고
합쳐지는 부분을 짜 놓았다. 인덱스 스테이지에서 다시 병합하는 것은 그
일을 중복하는 것이다. 우리가 할 일은 git이 사람에게 넘긴 구획들에서
선택을 받아 조립하는 것뿐이다.

순수 파이썬이다 (§3.1). 줄은 개행을 보존한 채 다룬다 — 마지막 줄의
개행 유무까지 왕복해야 "고른 것만 바뀐" 파일이 된다.
"""

from __future__ import annotations

from dataclasses import dataclass

CHOICE_OURS = "ours"
CHOICE_THEIRS = "theirs"
CHOICE_BOTH = "both"
CHOICES = (CHOICE_OURS, CHOICE_THEIRS, CHOICE_BOTH)


@dataclass(frozen=True, slots=True)
class ConflictHunk:
    """사람이 골라야 하는 구획 하나. 줄은 개행 포함."""

    ours: tuple[str, ...]
    theirs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CommonText:
    """git이 이미 합쳐둔 부분 — 선택 대상이 아니다."""

    lines: tuple[str, ...]


def parse_conflicted(text: str) -> tuple[object, ...]:
    """마커 파일을 (CommonText | ConflictHunk) 열로 가른다.

    diff3 스타일의 base 구획(`|||||||`~`=======`)은 읽고 버린다 — 선택지는
    내 것/상대 것/양쪽이고 base는 그 판단을 돕는 참고일 뿐인데, 이
    화면에서는 양쪽 내용 자체가 그 역할을 한다.

    마커가 어긋나면(닫히지 않음, 순서 역전) ValueError를 던진다 —
    **반쯤 합친 파일을 쓰는 것보다 거부가 낫다.** 사용자가 편집기로
    마커를 이미 손댔다는 뜻이므로 그 길로 마저 가면 된다.
    """
    segments: list[object] = []
    common: list[str] = []
    lines = text.splitlines(keepends=True)
    index = 0

    def flush_common() -> None:
        nonlocal common
        if common:
            segments.append(CommonText(tuple(common)))
            common = []

    while index < len(lines):
        line = lines[index]
        if _is_marker(line, "<<<<<<<"):
            flush_common()
            ours: list[str] = []
            theirs: list[str] = []
            index += 1
            index = _collect_until(lines, index, ours, ("|||||||", "======="))
            if index >= len(lines):
                raise ValueError("충돌 마커가 닫히지 않았습니다 (======= 없음)")
            if _is_marker(lines[index], "|||||||"):
                index += 1
                base: list[str] = []
                index = _collect_until(lines, index, base, ("=======",))
                if index >= len(lines):
                    raise ValueError("충돌 마커가 닫히지 않았습니다 (======= 없음)")
            index += 1  # ======= 를 지나
            index = _collect_until(lines, index, theirs, (">>>>>>>",))
            if index >= len(lines):
                raise ValueError("충돌 마커가 닫히지 않았습니다 (>>>>>>> 없음)")
            index += 1  # >>>>>>> 를 지나
            segments.append(ConflictHunk(tuple(ours), tuple(theirs)))
            continue
        if _is_marker(line, "=======") or _is_marker(line, ">>>>>>>"):
            raise ValueError("여는 마커 없이 닫는 마커가 나왔습니다")
        common.append(line)
        index += 1

    flush_common()
    return tuple(segments)


def hunks_of(segments: tuple[object, ...]) -> tuple[ConflictHunk, ...]:
    return tuple(s for s in segments if isinstance(s, ConflictHunk))


def compose(segments: tuple[object, ...], choices: list[str]) -> str:
    """선택대로 조립한다. 선택 수는 구획 수와 정확히 같아야 한다."""
    hunk_count = len(hunks_of(segments))
    if len(choices) != hunk_count:
        raise ValueError(
            f"선택이 {len(choices)}개인데 구획은 {hunk_count}개입니다"
        )
    unknown = [c for c in choices if c not in CHOICES]
    if unknown:
        raise ValueError(f"알 수 없는 선택: {unknown}")

    out: list[str] = []
    remaining = list(choices)
    for segment in segments:
        if isinstance(segment, CommonText):
            out.extend(segment.lines)
            continue
        choice = remaining.pop(0)
        if choice in (CHOICE_OURS, CHOICE_BOTH):
            out.extend(segment.ours)
        if choice in (CHOICE_THEIRS, CHOICE_BOTH):
            out.extend(segment.theirs)
    return "".join(out)


def _is_marker(line: str, marker: str) -> bool:
    stripped = line.rstrip("\r\n")
    return stripped == marker or stripped.startswith(marker + " ")


def _collect_until(
    lines: list[str], index: int, into: list[str], stops: tuple[str, ...]
) -> int:
    while index < len(lines) and not any(
        _is_marker(lines[index], stop) for stop in stops
    ):
        if _is_marker(lines[index], "<<<<<<<"):
            raise ValueError("충돌 마커가 중첩되었습니다")
        into.append(lines[index])
        index += 1
    return index
