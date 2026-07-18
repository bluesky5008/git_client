"""커밋 그래프 테이블 모델.

Qt의 뷰포트 가상화를 그대로 쓰기 위해 QAbstractTableModel을 상속한다.
행이 10만 개여도 실제로 그려지는 건 화면에 보이는 수십 개뿐이다.
(doc/design.md §2.2)

모델은 밀어넣기(push) 방식이다. 백그라운드 워커(application.commit_loader)가
커밋을 묶음으로 읽어 `append_commits()`로 전달하면, 모델은 그때마다 레인을
배치하고 행을 늘린다.

초안에서는 모델이 제너레이터를 직접 당겨오는(pull) 방식이었으나, 측정 결과
pygit2의 순회는 첫 커밋을 내놓기 전에 전체 히스토리를 읽으므로 지연 로딩이
성립하지 않았다. 비용을 없앨 수 없다면 최소한 UI 스레드 밖에서 치러야 한다.
"""

from __future__ import annotations

from datetime import datetime, timezone

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt
from PySide6.QtGui import QFont

from gitclient.domain.graph import GraphRow, LaneAllocator
from gitclient.domain.models import Commit, Ref


class CommitRole:
    """모델이 제공하는 커스텀 역할.

    Qt.UserRole부터 시작한다. 델리게이트가 그리기에 필요한 도메인 객체를
    문자열로 변환하지 않고 그대로 꺼내갈 수 있게 한다.
    """

    COMMIT = Qt.ItemDataRole.UserRole + 1
    GRAPH_ROW = Qt.ItemDataRole.UserRole + 2
    REFS = Qt.ItemDataRole.UserRole + 3


class Column:
    GRAPH = 0
    SUMMARY = 1
    AUTHOR = 2
    DATE = 3
    SHA = 4

    HEADERS = ("", "설명", "작성자", "날짜", "커밋")
    COUNT = len(HEADERS)


def format_relative(when: datetime) -> str:
    """사람이 읽기 쉬운 상대 시각. 오래된 것은 절대 날짜로 보여준다."""
    now = datetime.now(timezone.utc)
    delta = now - when.astimezone(timezone.utc)
    seconds = int(delta.total_seconds())

    if seconds < 0:
        # 시계 오차나 조작된 커밋 시각. 절대 시각으로 보여주는 편이 정직하다.
        return when.strftime("%Y-%m-%d %H:%M")
    if seconds < 60:
        return "방금"
    if seconds < 3600:
        return f"{seconds // 60}분 전"
    if seconds < 86400:
        return f"{seconds // 3600}시간 전"
    if seconds < 86400 * 7:
        return f"{seconds // 86400}일 전"
    return when.strftime("%Y-%m-%d")


class CommitGraphModel(QAbstractTableModel):
    """커밋 목록과 그래프 레이아웃을 함께 제공하는 모델."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._commits: list[Commit] = []
        self._rows: list[GraphRow] = []
        self._refs_by_sha: dict[str, list[Ref]] = {}
        self._row_by_sha: dict[str, int] = {}
        self._allocator = LaneAllocator()
        self._max_lane_count = 1

    # ------------------------------------------------------------------
    # 데이터 공급
    # ------------------------------------------------------------------

    def reset(self, refs: list[Ref]) -> None:
        """새 저장소를 위해 모델을 비운다. 커밋은 이후 묶음으로 들어온다."""
        self.beginResetModel()
        self._commits = []
        self._rows = []
        self._allocator = LaneAllocator()
        self._max_lane_count = 1
        self._row_by_sha = {}

        self._refs_by_sha = {}
        for ref in refs:
            self._refs_by_sha.setdefault(ref.target_sha, []).append(ref)

        self.endResetModel()

    def append_commits(self, commits: list[Commit]) -> None:
        """워커가 읽어온 묶음을 배치해 행으로 추가한다."""
        if not commits:
            return

        start = len(self._commits)
        self.beginInsertRows(QModelIndex(), start, start + len(commits) - 1)
        for commit in commits:
            row = self._allocator.push(commit.sha, commit.parents)
            self._row_by_sha[commit.sha] = len(self._commits)
            self._commits.append(commit)
            self._rows.append(row)
            self._max_lane_count = max(self._max_lane_count, row.lane_count)
        self.endInsertRows()

    def set_refs(self, refs: list[Ref]) -> None:
        """참조 목록을 (재)설정하고 로드된 행의 배지를 갱신한다.

        refs는 워커(RefsLoader)가 커밋 로딩과 병렬로 가져오므로,
        커밋이 이미 화면에 있는 상태에서 나중에 도착할 수 있다.
        """
        self._refs_by_sha = {}
        for ref in refs:
            self._refs_by_sha.setdefault(ref.target_sha, []).append(ref)

        if self._commits:
            # 배지는 SUMMARY 열에 그려진다. 로드된 전 구간을 갱신 대상으로 알린다.
            top_left = self.index(0, Column.SUMMARY)
            bottom_right = self.index(len(self._commits) - 1, Column.SUMMARY)
            self.dataChanged.emit(top_left, bottom_right, [CommitRole.REFS])

    @property
    def max_lane_count(self) -> int:
        """지금까지 로드된 범위에서 가장 넓은 행의 레인 수."""
        return self._max_lane_count

    def commit_at(self, row: int) -> Commit | None:
        if 0 <= row < len(self._commits):
            return self._commits[row]
        return None

    def row_for_sha(self, sha: str) -> int | None:
        """SHA가 로드된 행에 있으면 그 행 번호. 참조 클릭 → 커밋 이동에 쓴다."""
        return self._row_by_sha.get(sha)

    # ------------------------------------------------------------------
    # QAbstractTableModel 구현
    # ------------------------------------------------------------------

    def rowCount(self, parent: QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return len(self._commits)

    def columnCount(self, parent: QModelIndex | None = None) -> int:
        if parent is not None and parent.isValid():
            return 0
        return Column.COUNT

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ):
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation is not Qt.Orientation.Horizontal:
            return None
        if 0 <= section < Column.COUNT:
            return Column.HEADERS[section]
        return None

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        row = index.row()
        if not (0 <= row < len(self._commits)):
            return None

        commit = self._commits[row]

        if role == CommitRole.COMMIT:
            return commit
        if role == CommitRole.GRAPH_ROW:
            return self._rows[row]
        if role == CommitRole.REFS:
            return self._refs_by_sha.get(commit.sha, [])

        if role == Qt.ItemDataRole.DisplayRole:
            return self._display_text(commit, index.column())

        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(commit)

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if index.column() in (Column.DATE, Column.SHA):
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            return int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.FontRole and index.column() == Column.SHA:
            # SHA는 고정폭이어야 자릿수가 눈에 들어온다.
            font = QFont()
            font.setStyleHint(QFont.StyleHint.Monospace)
            font.setFamily("Consolas")
            return font

        return None

    def _display_text(self, commit: Commit, column: int) -> str:
        if column == Column.SUMMARY:
            return commit.summary
        if column == Column.AUTHOR:
            return commit.author.name
        if column == Column.DATE:
            return format_relative(commit.author.when)
        if column == Column.SHA:
            return commit.short_sha
        return ""

    def _tooltip(self, commit: Commit) -> str:
        when = commit.author.when.strftime("%Y-%m-%d %H:%M:%S %z")
        lines = [
            commit.summary,
            "",
            f"커밋:   {commit.sha}",
            f"작성자: {commit.author}",
            f"날짜:   {when}",
        ]
        if commit.body:
            lines.extend(["", commit.body])
        return "\n".join(lines)
