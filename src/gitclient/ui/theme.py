"""색상 팔레트.

레인의 1차 구분 수단은 색이 아니라 **가로 위치**이며, 색상(hue)은 정상 색각
사용자용 보조 단서다. 현재 팔레트는 색각 이상(CVD) 완전 구분과 명도 대비를
확보하지 못했다 — 실측 최악 쌍은 초록 #5aab61 / 청록 #3fa9a0으로 상대휘도
대비비 1.005:1(그레이스케일에서 동일)이고, 하필 녹-청록은 흔한 색각 이상에서
혼동되는 조합이다. Phase 5에서 CVD 시뮬레이션으로 정량화한 뒤 검증된 팔레트
또는 색 외 중복 부호화로 보강한다. (doc/design.md §5.3)

도메인 층은 색상 인덱스만 다루고, 실제 색상값은 여기서 결정한다.
"""

from __future__ import annotations

from PySide6.QtGui import QColor

from gitclient.domain.graph import PALETTE_SIZE


def relative_luminance(color: QColor) -> float:
    """WCAG 2.x 상대휘도."""

    def channel(value: int) -> float:
        c = value / 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return (
        0.2126 * channel(color.red())
        + 0.7152 * channel(color.green())
        + 0.0722 * channel(color.blue())
    )


def contrast_ratio(a: QColor, b: QColor) -> float:
    """WCAG 대비비. 1(동일)~21(흑백)."""
    la, lb = relative_luminance(a), relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)

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


_BADGE_TEXT_LIGHT = QColor("#ffffff")
_BADGE_TEXT_DARK = QColor("#15181c")


def badge_text_color(background: QColor) -> QColor:
    """배지 배경 위 글자색 — 흰색/진회색 중 대비가 큰 쪽.

    초안은 흰 글자를 고정으로 썼는데, 중간 명도 배지에서 WCAG AA(4.5:1)에
    미달했다(TAG 주황 위 흰 글자 2.46:1). 배경 휘도로 선택하면 현 팔레트
    전부에서 AA를 만족하고, 팔레트가 바뀌어도 따로 손댈 필요가 없다.
    """
    if contrast_ratio(background, _BADGE_TEXT_DARK) >= contrast_ratio(
        background, _BADGE_TEXT_LIGHT
    ):
        return _BADGE_TEXT_DARK
    return _BADGE_TEXT_LIGHT
