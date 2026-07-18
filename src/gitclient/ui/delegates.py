"""커스텀 페인팅 델리게이트.

Qt의 기본 아이템 렌더링으로는 표현할 수 없는 세 가지를 직접 그린다.

  - 커밋 그래프의 레인과 곡선 (GraphDelegate)
  - 브랜치/태그 배지 (SummaryDelegate)
  - diff의 줄 배경과 줄 번호 (DiffDelegate)

(doc/design.md §4.1, §4.2)
"""

from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPointF, QRect, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from gitclient.domain.graph import EdgeKind, GraphRow
from gitclient.domain.models import DiffLine, DiffLineKind, Ref, RefKind
from gitclient.ui.theme import DiffColors, RefColors, lane_color
from gitclient.viewmodel.commit_graph_model import CommitRole
from gitclient.viewmodel.diff_model import DiffRole

LANE_WIDTH = 16
"""레인 하나가 차지하는 가로 픽셀."""

NODE_RADIUS = 4.0
MERGE_NODE_RADIUS = 5.0
EDGE_WIDTH = 2.0


class GraphDelegate(QStyledItemDelegate):
    """커밋 그래프 열을 그린다.

    한 행에서 그리는 것은 세 가지다.

      PASS      이 커밋과 무관한 브랜치의 선. 위에서 아래로 수직 통과.
      INCOMING  위에서 내려와 이 커밋 노드로 합류하는 선.
      OUTGOING  이 커밋 노드에서 아래로 뻗어나가는 선.

    레인이 바뀌는 선은 베지어 곡선으로 그려 GitKraken처럼 부드럽게 보이게 한다.
    """

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        graph_row: GraphRow | None = index.data(CommitRole.GRAPH_ROW)
        if graph_row is None:
            super().paint(painter, option, index)
            return

        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = option.rect
        top = float(rect.top())
        bottom = float(rect.bottom() + 1)
        center_y = (top + bottom) / 2.0
        node_x = self._lane_x(rect, graph_row.lane)

        for edge in graph_row.edges:
            color = lane_color(edge.color)
            painter.setPen(
                QPen(color, EDGE_WIDTH, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            )

            from_x = self._lane_x(rect, edge.from_lane)
            to_x = self._lane_x(rect, edge.to_lane)

            if edge.kind is EdgeKind.PASS:
                painter.drawLine(QPointF(from_x, top), QPointF(from_x, bottom))
            elif edge.kind is EdgeKind.INCOMING:
                self._draw_curve(
                    painter, QPointF(from_x, top), QPointF(to_x, center_y)
                )
            else:  # OUTGOING
                self._draw_curve(
                    painter, QPointF(from_x, center_y), QPointF(to_x, bottom)
                )

        self._draw_node(painter, node_x, center_y, graph_row, option)
        painter.restore()

    def _draw_curve(self, painter: QPainter, start: QPointF, end: QPointF) -> None:
        """두 점을 잇는다. 레인이 같으면 직선, 다르면 S자 곡선."""
        if abs(start.x() - end.x()) < 0.5:
            painter.drawLine(start, end)
            return

        path = QPainterPath(start)
        mid_y = (start.y() + end.y()) / 2.0
        path.cubicTo(
            QPointF(start.x(), mid_y),
            QPointF(end.x(), mid_y),
            end,
        )
        painter.strokePath(path, painter.pen())

    def _draw_node(
        self,
        painter: QPainter,
        x: float,
        y: float,
        graph_row: GraphRow,
        option: QStyleOptionViewItem,
    ) -> None:
        color = lane_color(graph_row.color)
        radius = MERGE_NODE_RADIUS if graph_row.is_merge else NODE_RADIUS

        # 배경색으로 테두리를 둘러 선이 노드를 관통하는 것처럼 보이지 않게 한다.
        painter.setPen(QPen(option.palette.base().color(), 2.0))
        painter.setBrush(color)
        painter.drawEllipse(QPointF(x, y), radius, radius)

        if graph_row.is_merge:
            # 머지 커밋은 가운데를 비워 한눈에 구분되게 한다.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(option.palette.base().color())
            painter.drawEllipse(QPointF(x, y), radius - 2.0, radius - 2.0)

    def _lane_x(self, rect: QRect, lane: int) -> float:
        return rect.left() + LANE_WIDTH * (lane + 0.5)

    def sizeHint(
        self, option: QStyleOptionViewItem, index: QModelIndex
    ) -> QSize:
        graph_row: GraphRow | None = index.data(CommitRole.GRAPH_ROW)
        lanes = graph_row.lane_count if graph_row else 1
        base = super().sizeHint(option, index)
        return QSize(LANE_WIDTH * lanes, base.height())


class SummaryDelegate(QStyledItemDelegate):
    """커밋 설명 앞에 브랜치/태그 배지를 그린다."""

    BADGE_PADDING = 5
    BADGE_SPACING = 4
    BADGE_RADIUS = 3

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        refs: list[Ref] = index.data(CommitRole.REFS) or []
        if not refs:
            super().paint(painter, option, index)
            return

        self.initStyleOption(option, index)
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
            text_color = option.palette.highlightedText().color()
        else:
            text_color = option.palette.text().color()

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        badge_font = QFont(option.font)
        badge_font.setPointSizeF(max(6.5, option.font.pointSizeF() - 1.0))
        metrics = QFontMetrics(badge_font)

        x = option.rect.left() + 4
        available_right = option.rect.right() - 4

        for ref in refs:
            label = ref.shorthand
            width = metrics.horizontalAdvance(label) + self.BADGE_PADDING * 2
            if x + width > available_right:
                break

            height = metrics.height() + 2
            badge_rect = QRect(
                x,
                option.rect.center().y() - height // 2,
                width,
                height,
            )

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(self._badge_color(ref))
            painter.drawRoundedRect(
                badge_rect, self.BADGE_RADIUS, self.BADGE_RADIUS
            )

            painter.setFont(badge_font)
            painter.setPen(RefColors.TEXT)
            painter.drawText(
                badge_rect, Qt.AlignmentFlag.AlignCenter, label
            )

            x += width + self.BADGE_SPACING

        text_rect = QRect(
            x + 2,
            option.rect.top(),
            max(0, option.rect.right() - x - 2),
            option.rect.height(),
        )
        painter.setFont(option.font)
        painter.setPen(text_color)
        elided = QFontMetrics(option.font).elidedText(
            index.data(Qt.ItemDataRole.DisplayRole) or "",
            Qt.TextElideMode.ElideRight,
            text_rect.width(),
        )
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            elided,
        )
        painter.restore()

    def _badge_color(self, ref: Ref) -> QColor:
        if ref.is_head:
            return RefColors.HEAD
        if ref.kind is RefKind.LOCAL_BRANCH:
            return RefColors.LOCAL_BRANCH
        if ref.kind is RefKind.TAG:
            return RefColors.TAG
        return RefColors.REMOTE_BRANCH


class DiffDelegate(QStyledItemDelegate):
    """diff 한 줄을 그린다. 줄 번호와 배경색을 함께 처리한다."""

    LINENO_WIDTH = 44
    GUTTER = 6

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index: QModelIndex,
    ) -> None:
        line: DiffLine | None = index.data(DiffRole.LINE)
        if line is None:
            super().paint(painter, option, index)
            return

        painter.save()
        rect = option.rect

        background = self._background(line.kind)
        if background is not None:
            painter.fillRect(rect, background)

        if line.kind is DiffLineKind.FILE_HEADER:
            self._paint_header(painter, option, line.text, bold=True)
            painter.restore()
            return

        if line.kind is DiffLineKind.HUNK_HEADER:
            self._paint_header(painter, option, line.text, bold=False)
            painter.restore()
            return

        self._paint_lineno(painter, option, line)

        text_rect = QRect(
            rect.left() + self.LINENO_WIDTH * 2 + self.GUTTER,
            rect.top(),
            rect.width() - self.LINENO_WIDTH * 2 - self.GUTTER,
            rect.height(),
        )
        painter.setFont(option.font)
        painter.setPen(self._text_color(line.kind, option))
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            f"{self._marker(line.kind)}{line.text}",
        )
        painter.restore()

    def _paint_header(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        text: str,
        *,
        bold: bool,
    ) -> None:
        font = QFont(option.font)
        font.setBold(bold)
        painter.setFont(font)
        painter.setPen(
            DiffColors.FILE_HEADER_FG if bold else DiffColors.HUNK_FG
        )
        painter.drawText(
            option.rect.adjusted(self.GUTTER, 0, 0, 0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            text,
        )

    def _paint_lineno(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        line: DiffLine,
    ) -> None:
        painter.setFont(option.font)
        painter.setPen(DiffColors.LINENO_FG)
        rect = option.rect

        for offset, value in enumerate((line.old_lineno, line.new_lineno)):
            if value is None:
                continue
            cell = QRect(
                rect.left() + self.LINENO_WIDTH * offset,
                rect.top(),
                self.LINENO_WIDTH - 4,
                rect.height(),
            )
            painter.drawText(
                cell,
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                str(value),
            )

    def _background(self, kind: DiffLineKind) -> QColor | None:
        return {
            DiffLineKind.ADDITION: DiffColors.ADDITION_BG,
            DiffLineKind.DELETION: DiffColors.DELETION_BG,
            DiffLineKind.HUNK_HEADER: DiffColors.HUNK_BG,
            DiffLineKind.FILE_HEADER: DiffColors.FILE_HEADER_BG,
        }.get(kind)

    def _text_color(
        self, kind: DiffLineKind, option: QStyleOptionViewItem
    ) -> QColor:
        if kind is DiffLineKind.ADDITION:
            return DiffColors.ADDITION_MARK
        if kind is DiffLineKind.DELETION:
            return DiffColors.DELETION_MARK
        return option.palette.text().color()

    def _marker(self, kind: DiffLineKind) -> str:
        if kind is DiffLineKind.ADDITION:
            return "+ "
        if kind is DiffLineKind.DELETION:
            return "- "
        return "  "
