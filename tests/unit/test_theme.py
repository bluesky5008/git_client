"""테마 접근성 테스트.

배지 글자/배경 대비가 WCAG AA(4.5:1)를 만족하는지 계산으로 검증한다.
눈대중 판정은 금지다 — doc/design.md §4.1.1.3 교훈.
"""

from __future__ import annotations

import pytest
from PySide6.QtGui import QColor

from gitclient.domain.graph import PALETTE_SIZE
from gitclient.ui.theme import (
    LANE_COLORS,
    RefColors,
    badge_text_color,
    contrast_ratio,
    lane_color,
    relative_luminance,
)

WCAG_AA = 4.5

BADGE_BACKGROUNDS = {
    "local_branch": RefColors.LOCAL_BRANCH,
    "remote_branch": RefColors.REMOTE_BRANCH,
    "tag": RefColors.TAG,
    "head": RefColors.HEAD,
}


class TestContrastMath:
    def test_black_on_white_is_21(self) -> None:
        assert contrast_ratio(QColor("#000000"), QColor("#ffffff")) == pytest.approx(
            21.0, abs=0.01
        )

    def test_same_color_is_1(self) -> None:
        assert contrast_ratio(QColor("#4a90d9"), QColor("#4a90d9")) == pytest.approx(
            1.0
        )

    def test_luminance_bounds(self) -> None:
        assert relative_luminance(QColor("#000000")) == pytest.approx(0.0)
        assert relative_luminance(QColor("#ffffff")) == pytest.approx(1.0)


class TestBadgeContrast:
    @pytest.mark.parametrize("name", BADGE_BACKGROUNDS)
    def test_badge_text_meets_wcag_aa(self, name: str) -> None:
        background = BADGE_BACKGROUNDS[name]
        text = badge_text_color(background)
        assert contrast_ratio(text, background) >= WCAG_AA, (
            f"{name} 배지의 글자 대비가 AA 미달"
        )

    def test_fixed_white_would_have_failed(self) -> None:
        # 이 수정이 실제 문제를 고쳤음을 문서화하는 테스트.
        # 고정 흰 글자는 TAG 배지에서 AA에 크게 미달했다.
        assert contrast_ratio(QColor("#ffffff"), RefColors.TAG) < WCAG_AA


class TestLanePalette:
    def test_palette_size_matches_domain(self) -> None:
        assert len(LANE_COLORS) == PALETTE_SIZE

    def test_lane_color_wraps(self) -> None:
        assert lane_color(PALETTE_SIZE) == lane_color(0)

    def test_known_cvd_limitation_is_still_present(self) -> None:
        """§5.3에 기록된 한계가 실제임을 고정한다.

        이 테스트는 팔레트가 '좋다'가 아니라 '문서와 일치한다'를 검증한다.
        Phase 5에서 팔레트를 재설계하면 이 테스트를 새 보장으로 교체할 것.
        """
        green, teal = QColor("#5aab61"), QColor("#3fa9a0")
        assert contrast_ratio(green, teal) < 1.05  # 사실상 명도 동일
