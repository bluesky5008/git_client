"""색상 팔레트.

그래프 레인 색상은 색각 이상을 고려해 색상(hue)뿐 아니라 명도도 서로 다르게
잡았다. 색상만으로 라인을 구분하게 만들지 않는다. (doc/design.md §5.3)

도메인 층은 색상 인덱스만 다루고, 실제 색상값은 여기서 결정한다.
"""

from __future__ import annotations

from PySide6.QtGui import QColor

from gitclient.domain.graph import PALETTE_SIZE

# 레인 색상. graph.PALETTE_SIZE와 길이가 같아야 한다.
LANE_COLORS: tuple[QColor, ...] = (
    QColor("#4a90d9"),  # 파랑
    QColor("#e8913a"),  # 주황
    QColor("#5aab61"),  # 초록
    QColor("#c2504a"),  # 빨강
    QColor("#9068be"),  # 보라
    QColor("#3fa9a0"),  # 청록
    QColor("#c9689b"),  # 분홍
    QColor("#8a8f4a"),  # 올리브
)

assert len(LANE_COLORS) == PALETTE_SIZE, "팔레트 크기가 도메인 상수와 어긋납니다"


def lane_color(index: int) -> QColor:
    return LANE_COLORS[index % len(LANE_COLORS)]


class DiffColors:
    ADDITION_BG = QColor(70, 149, 74, 45)
    DELETION_BG = QColor(194, 80, 74, 45)
    ADDITION_MARK = QColor("#5aab61")
    DELETION_MARK = QColor("#c2504a")
    HUNK_BG = QColor(128, 128, 128, 30)
    HUNK_FG = QColor("#7a8290")
    FILE_HEADER_BG = QColor(74, 144, 217, 40)
    FILE_HEADER_FG = QColor("#4a90d9")
    LINENO_FG = QColor("#8a8f98")


class RefColors:
    """브랜치/태그 배지 색상."""

    LOCAL_BRANCH = QColor("#4a90d9")
    REMOTE_BRANCH = QColor("#8a8f98")
    TAG = QColor("#e8913a")
    HEAD = QColor("#5aab61")
    TEXT = QColor("#ffffff")
