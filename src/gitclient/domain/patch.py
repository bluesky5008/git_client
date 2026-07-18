"""패치 합성 — 헝크/라인 단위 스테이징의 핵심.

부분 스테이징은 "선택된 변경만 담은 패치 텍스트를 만들어 인덱스에 적용"하는
방식으로 동작한다. (doc/design.md §4.2)

이 모듈은 순수 파이썬이다. Qt/pygit2에 의존하지 않으므로 어려운 부분
(줄 번호 재계산, 미선택 삭제줄의 컨텍스트 전환, EOF 개행 처리)을 전부
단위 테스트로 고정할 수 있다.

**두 방향의 선택 의미론은 서로 거울상이다.** 어느 쪽이든 만들어진 패치는
**인덱스에 적용**되므로, 컨텍스트가 인덱스 내용과 일치해야 한다.

스테이징(forward) — 원본 diff는 인덱스→워킹트리다:

  선택된 '+'    유지 ('+')      인덱스에 새로 넣을 줄
  미선택 '+'    **버린다**       인덱스에 아직 없어야 하는 줄
  선택된 '-'    유지 ('-')      인덱스에서 지울 줄
  미선택 '-'    **컨텍스트로**   인덱스에 아직 남아 있는 줄

언스테이징(reverse) — 원본 diff는 HEAD→인덱스다:

  선택된 '+'    **'-'로 뒤집음**  인덱스에서 되돌릴 줄
  미선택 '+'    **컨텍스트로**    인덱스에 그대로 남는 줄
  선택된 '-'    **'+'로 뒤집음**  인덱스에 되살릴 줄
  미선택 '-'    **버린다**        인덱스에 없는 줄

방향마다 "버리기"와 "컨텍스트로 바꾸기"의 대상이 정반대다. 이걸 뒤바꾸면
인덱스에 없는 줄을 컨텍스트로 요구하게 되어 `hunk did not apply`로 거부된다.

시작 줄 번호도 방향에 따라 다른 쪽을 쓴다 — 패치가 적용될 파일(=인덱스)의
번호여야 하므로 forward는 `old_start`, reverse는 `new_start`가 기준이다.

**안전성**: 합성이 틀리면 libgit2가 "invalid patch hunk" 또는 "hunk did not
apply"로 거부한다(실측 확인). 조용히 인덱스를 오염시키지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# libgit2 diff line origin 문자
ORIGIN_CONTEXT = " "
ORIGIN_ADDITION = "+"
ORIGIN_DELETION = "-"

# EOF 개행 관련 마커. 짝이 되는 줄 바로 뒤에 별도 줄로 따라오며,
# 헝크의 줄 수 계산에 포함되지 않는다.
ORIGIN_CONTEXT_EOFNL = "="
ORIGIN_ADD_EOFNL = ">"
ORIGIN_DEL_EOFNL = "<"

EOFNL_ORIGINS = frozenset({ORIGIN_CONTEXT_EOFNL, ORIGIN_ADD_EOFNL, ORIGIN_DEL_EOFNL})


class PatchError(Exception):
    """패치를 합성할 수 없는 상태."""


@dataclass(frozen=True, slots=True)
class PatchLine:
    """패치의 한 줄.

    `content`는 **가공하지 않은 원문**이다. 줄바꿈 문자를 포함하며,
    EOF 개행이 없는 줄은 줄바꿈 없이 끝난다. 표시용으로 다듬은 문자열이
    아니므로 절대 strip 하지 말 것 — 그대로 이어붙여야 유효한 패치가 된다.
    """

    origin: str
    content: str
    old_lineno: int | None = None
    new_lineno: int | None = None

    @property
    def is_eofnl_marker(self) -> bool:
        return self.origin in EOFNL_ORIGINS

    @property
    def is_change(self) -> bool:
        """사용자가 선택/해제할 수 있는 줄인가."""
        return self.origin in (ORIGIN_ADDITION, ORIGIN_DELETION)


@dataclass(frozen=True, slots=True)
class PatchHunk:
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    lines: tuple[PatchLine, ...] = ()

    @property
    def header(self) -> str:
        return format_hunk_header(
            self.old_start, self.old_lines, self.new_start, self.new_lines
        )

    @property
    def change_count(self) -> int:
        return sum(1 for line in self.lines if line.is_change)


@dataclass(frozen=True, slots=True)
class FilePatch:
    """파일 하나의 패치."""

    old_path: str
    new_path: str
    hunks: tuple[PatchHunk, ...] = ()
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    old_mode: str = "100644"
    new_mode: str = "100644"

    @property
    def is_rename(self) -> bool:
        return self.old_path != self.new_path

    @property
    def can_stage_partially(self) -> bool:
        """부분 스테이징이 가능한가.

        바이너리는 줄 개념이 없어 불가능하다. 이름 변경은 두 경로를 함께
        옮겨야 하는데 부분 패치는 한 경로만 다루므로 절반만 적용된다 —
        파일 단위 조작으로 처리해야 한다.
        """
        return not self.is_binary and not self.is_rename and bool(self.hunks)


def iter_display_rows(
    patch: FilePatch,
) -> list[tuple[str, str, int | None, int | None, tuple[int, int] | None]]:
    """화면에 그릴 행을 (종류, 텍스트, old줄번호, new줄번호, 좌표)로 펼친다.

    좌표는 `(헝크 인덱스, 줄 인덱스)`이며 변경 줄에만 붙는다. 화면의 줄과
    패치의 줄이 같은 좌표를 공유해야 사용자가 고른 것이 그대로 적용된다 —
    표시와 적용이 각자 계산하면 어긋날 수 있다.

    종류는 "header" | "hunk" | "context" | "add" | "del" 이다.
    EOF 개행 마커는 화면에 별도 줄로 만들지 않는다 (짝 줄에 딸린 정보다).
    """
    rows: list[tuple[str, str, int | None, int | None, tuple[int, int] | None]] = []
    label = (
        f"{patch.old_path} -> {patch.new_path}"
        if patch.old_path != patch.new_path
        else patch.new_path
    )
    rows.append(("header", label, None, None, None))

    if patch.is_binary:
        rows.append(
            ("context", "(바이너리 파일 — 내용 diff를 표시할 수 없습니다)", None, None, None)
        )
        return rows

    for hunk_index, hunk in enumerate(patch.hunks):
        rows.append(("hunk", hunk.header, None, None, None))
        for line_index, line in enumerate(hunk.lines):
            if line.is_eofnl_marker:
                continue
            text = line.content.rstrip("\r\n")
            if line.origin == ORIGIN_ADDITION:
                kind = "add"
            elif line.origin == ORIGIN_DELETION:
                kind = "del"
            else:
                kind = "context"
            position = (hunk_index, line_index) if line.is_change else None
            rows.append((kind, text, line.old_lineno, line.new_lineno, position))

    return rows


def format_hunk_header(
    old_start: int, old_lines: int, new_start: int, new_lines: int
) -> str:
    """`@@ -a,b +c,d @@` 형태. git은 줄 수가 1이면 개수를 생략한다.

    생략 형식을 따르는 이유는 미학이 아니라 호환성이다 — 일부 도구가
    `-a,1` 대신 `-a`를 기대한다. libgit2는 둘 다 받아들이지만
    git이 만드는 것과 같은 형태를 내는 편이 진단할 때 헷갈리지 않는다.
    """
    old = f"{old_start}" if old_lines == 1 else f"{old_start},{old_lines}"
    new = f"{new_start}" if new_lines == 1 else f"{new_start},{new_lines}"
    return f"@@ -{old} +{new} @@"


def _selected_key(hunk_index: int, line_index: int) -> tuple[int, int]:
    return (hunk_index, line_index)


@dataclass
class _HunkBuild:
    """합성 중인 헝크 하나의 누적 상태."""

    old_count: int = 0
    new_count: int = 0
    body: list[str] = field(default_factory=list)

    @property
    def has_change(self) -> bool:
        return any(line[:1] in (ORIGIN_ADDITION, ORIGIN_DELETION) for line in self.body)


def synthesize_patch(
    patch: FilePatch,
    selected: set[tuple[int, int]] | None = None,
    *,
    reverse: bool = False,
) -> str:
    """선택된 줄만 담은 패치 텍스트를 만든다.

    `selected`는 `(헝크 인덱스, 줄 인덱스)` 집합이다. None이면 모든 변경 줄을
    선택한 것으로 본다(헝크 전체 스테이징).

    `reverse=True`면 적용 방향을 뒤집는다. 스테이징된 변경을 인덱스에서
    되돌릴 때(언스테이징) 쓴다.

    선택된 변경이 하나도 없으면 PatchError를 던진다 — 빈 패치를 적용하면
    아무 일도 일어나지 않는데 UI는 성공했다고 표시하게 되므로,
    조용히 넘어가는 대신 실패로 알린다.
    """
    if patch.is_binary:
        raise PatchError(
            "바이너리 파일은 부분 스테이징할 수 없습니다. 파일 단위로 처리해 주세요."
        )
    if patch.is_rename:
        # 이름 변경은 옛 경로 삭제와 새 경로 추가가 한 쌍이다. 부분 패치는
        # 한 경로만 다루므로 적용하면 한쪽만 반영되어 인덱스가 어긋난다.
        raise PatchError(
            f"이름이 바뀐 파일({patch.old_path} → {patch.new_path})은 "
            "부분 스테이징할 수 없습니다. 파일 단위로 처리해 주세요."
        )
    if not patch.hunks:
        raise PatchError("적용할 변경이 없습니다.")

    builds: list[tuple[int, _HunkBuild]] = []

    for hunk_index, hunk in enumerate(patch.hunks):
        _reject_ambiguous_selection(hunk, hunk_index, selected)
        build = _HunkBuild()
        pending_marker: str | None = None

        for line_index, line in enumerate(hunk.lines):
            if line.is_eofnl_marker:
                # 마커는 짝이 되는 줄의 운명을 따른다. 앞선 줄이 살아남았을
                # 때만 함께 나간다.
                if pending_marker is not None:
                    build.body.append(pending_marker)
                    pending_marker = None
                continue

            keep = (
                True
                if selected is None
                else _selected_key(hunk_index, line_index) in selected
            )

            emitted = _emit_line(line, keep=keep, build=build, reverse=reverse)
            pending_marker = None
            if emitted is None:
                continue
            build.body.append(emitted)
            # 다음 줄이 마커라면 이 줄의 뒤에 붙어야 한다.
            pending_marker = _lookahead_marker(hunk, line_index)

        if build.has_change:
            builds.append((hunk_index, build))

    if not builds:
        raise PatchError("선택된 변경이 없습니다. 스테이징할 줄을 선택해 주세요.")

    return _render(patch, builds, reverse=reverse)


def _reject_ambiguous_selection(
    hunk: PatchHunk, hunk_index: int, selected: set[tuple[int, int]] | None
) -> None:
    """줄 순서가 뒤바뀔 수 있는 선택을 거부한다.

    diff는 인접한 변경을 "삭제 묶음 + 추가 묶음"으로 그룹화한다
    (`-b -c +B +c`). 이런 묶음에서 삭제 일부만 빼고 추가를 고르면, 컨텍스트로
    바뀐 삭제줄이 추가줄보다 앞에 놓여 **내용 순서가 뒤바뀐다** — 패치 자체는
    유효해서 libgit2가 받아주므로 조용한 손상이 된다.

    묶음의 삭제가 한 줄뿐이면 대응이 유일해 안전하다. 여러 줄이 묶여 있고
    그중 일부만 고른 경우에만 거부한다. 헝크 전체 스테이징은 언제나 안전하다.
    """
    if selected is None:
        return  # 전체 선택 — 원본 순서 그대로라 문제없다

    for start, deletions, additions in _change_blocks(hunk):
        del_keys = [(hunk_index, start + i) for i in range(len(deletions))]
        add_keys = [
            (hunk_index, start + len(deletions) + i) for i in range(len(additions))
        ]
        if len(deletions) <= 1:
            continue
        unselected_deletion = any(key not in selected for key in del_keys)
        selected_addition = any(key in selected for key in add_keys)
        if unselected_deletion and selected_addition:
            raise PatchError(
                "이 변경 묶음은 줄 단위로 나눌 수 없습니다. "
                "여러 줄이 한 덩어리로 바뀌어 일부만 고르면 순서가 어긋납니다.\n"
                "헝크 전체를 스테이징해 주세요."
            )


def _change_blocks(
    hunk: PatchHunk,
) -> list[tuple[int, list[PatchLine], list[PatchLine]]]:
    """헝크를 (시작 인덱스, 삭제줄들, 추가줄들) 묶음으로 나눈다.

    EOF 마커는 앞 줄에 딸린 것이라 묶음 경계를 끊지 않는다.
    """
    blocks: list[tuple[int, list[PatchLine], list[PatchLine]]] = []
    index = 0
    lines = hunk.lines

    while index < len(lines):
        if not lines[index].is_change:
            index += 1
            continue

        start = index
        deletions: list[PatchLine] = []
        additions: list[PatchLine] = []
        while index < len(lines) and (
            lines[index].is_change or lines[index].is_eofnl_marker
        ):
            line = lines[index]
            if line.origin == ORIGIN_DELETION:
                deletions.append(line)
            elif line.origin == ORIGIN_ADDITION:
                additions.append(line)
            index += 1
        blocks.append((start, deletions, additions))

    return blocks


def _lookahead_marker(hunk: PatchHunk, line_index: int) -> str | None:
    """다음 줄이 EOF 개행 마커면 그 원문을 돌려준다.

    마커는 origin 접두사 없이 content만 붙는다. content가 개행으로 시작해
    앞 줄을 끝맺으므로, 그대로 이어붙이면 `-y\\n\\ No newline...` 형태가 된다.
    """
    nxt = line_index + 1
    if nxt < len(hunk.lines) and hunk.lines[nxt].is_eofnl_marker:
        return hunk.lines[nxt].content
    return None


def _emit_line(
    line: PatchLine, *, keep: bool, build: _HunkBuild, reverse: bool
) -> str | None:
    """줄 하나를 **최종 방향의** 패치 본문으로 바꾸고 줄 수를 누적한다.

    반환값 None은 "이 줄은 패치에 포함하지 않는다"는 뜻이다.
    """
    if line.origin == ORIGIN_CONTEXT:
        build.old_count += 1
        build.new_count += 1
        return ORIGIN_CONTEXT + line.content

    if line.origin not in (ORIGIN_ADDITION, ORIGIN_DELETION):
        raise PatchError(f"알 수 없는 diff 줄 종류입니다: {line.origin!r}")

    # 이 줄이 인덱스에 이미 존재하는가.
    #   forward: 원본이 인덱스→워킹트리이므로 '-'가 인덱스에 있다.
    #   reverse: 원본이 HEAD→인덱스이므로 '+'가 인덱스에 있다.
    in_index = (
        line.origin == ORIGIN_ADDITION if reverse else line.origin == ORIGIN_DELETION
    )

    if not keep:
        if in_index:
            # 인덱스에 남아 있어야 하는 줄 → 컨텍스트.
            build.old_count += 1
            build.new_count += 1
            return ORIGIN_CONTEXT + line.content
        # 인덱스에 없는 줄 → 패치에서 통째로 뺀다.
        return None

    if in_index:
        # 인덱스에서 없앤다.
        build.old_count += 1
        return ORIGIN_DELETION + line.content

    # 인덱스에 넣는다.
    build.new_count += 1
    return ORIGIN_ADDITION + line.content


def _render(
    patch: FilePatch,
    builds: list[tuple[int, _HunkBuild]],
    *,
    reverse: bool,
) -> str:
    """헤더와 헝크를 합쳐 최종 패치 텍스트를 만든다."""
    is_new, is_deleted = patch.is_new, patch.is_deleted
    if reverse:
        is_new, is_deleted = is_deleted, is_new

    # 패치는 **인덱스에** 적용된다. 인덱스 쪽 경로는 forward가 old, reverse가 new다.
    # 양쪽을 이 경로 하나로 고정한다: 이름 변경 델타에서 a/ b/ 를 다른 경로로 쓰면
    # libgit2가 `rename from/to` 없는 별개 델타로 처리해 원래 경로를 인덱스에서
    # 지우지 않아, 두 경로가 동시에 스테이징된 채 남는다. 부분 스테이징이 다루는
    # 것은 내용 줄뿐이므로 이름 변경 자체는 건드리지 않는다.
    path = patch.new_path if reverse else patch.old_path

    # 인덱스 쪽 모드. reverse면 인덱스가 new 쪽이므로 new_mode가 기준이다.
    # 삭제 헤더에만 쓴다 — `old mode`/`new mode` 헤더는 libgit2가
    # `invalid patch header`로 거부하므로 넣지 않는다. 실행 비트 보존은
    # 적용 후 인덱스에서 되돌리는 방식으로 처리한다(local_engine).
    mode = patch.new_mode if reverse else patch.old_mode

    # 부분 선택이면 파일 삭제 헤더를 써서는 안 된다. libgit2는 파일 수준 헤더를
    # 본문보다 우선해 인덱스 엔트리를 통째로 지우므로, 선택하지 않은 줄까지
    # 오류 없이 사라진다. 결과 쪽에 줄이 하나라도 남으면 삭제가 아니라 수정이다.
    #
    # `len(builds) == len(patch.hunks)` 조건이 필요한 이유: 어떤 헝크에서 아무것도
    # 선택하지 않으면 그 헝크가 통째로 빠지는데, 남은 헝크만 보면 new_count == 0이라
    # 삭제로 오판할 수 있다.
    if is_deleted:
        deletes_everything = len(builds) == len(patch.hunks) and all(
            build.new_count == 0 for _, build in builds
        )
        if not deletes_everything:
            is_deleted = False

    # `is_new`는 대칭으로 강등하지 않는다 — 일부만 골라도 인덱스에 없던 파일이
    # 새로 생기는 것은 사실이고, git/libgit2 모두 정상 처리한다.

    out: list[str] = [f"diff --git a/{path} b/{path}\n"]
    if is_new:
        out.append(f"new file mode {patch.new_mode}\n")
    elif is_deleted:
        out.append(f"deleted file mode {mode}\n")

    out.append(f"--- {'/dev/null' if is_new else f'a/{path}'}\n")
    out.append(f"+++ {'/dev/null' if is_deleted else f'b/{path}'}\n")

    # 본문은 이미 최종 방향으로 만들어져 있다. 남은 것은 시작 번호 계산뿐이다.
    #
    # old_start는 **패치가 적용될 파일(인덱스)** 의 줄 번호여야 한다.
    #   forward: 원본 diff의 old가 인덱스 → source.old_start
    #   reverse: 원본 diff의 new가 인덱스 → source.new_start
    # 앞선 헝크가 줄 수를 바꾸면 결과 쪽(new_start)이 그만큼 밀린다.
    offset = 0
    for hunk_index, build in builds:
        source = patch.hunks[hunk_index]
        old_count, new_count = build.old_count, build.new_count
        old_start = source.new_start if reverse else source.old_start

        if old_count == 0:
            # 적용 대상에 대응하는 줄이 없는 순수 삽입. git 관례상 old_start는
            # "이 줄 뒤에 끼워 넣는다"는 뜻이고 결과는 그다음 줄에서 시작한다.
            # (`@@ -3,0 +4,2 @@`, 새 파일이면 `@@ -0,0 +1,n @@`)
            new_start = old_start + 1 + offset
        else:
            new_start = old_start + offset

        if new_count == 0:
            # 결과 쪽에 줄이 없는 순수 삭제 (파일 삭제 포함).
            new_start = 0

        offset += new_count - old_count

        out.append(
            format_hunk_header(old_start, old_count, new_start, new_count) + "\n"
        )
        out.extend(build.body)

    return "".join(out)
