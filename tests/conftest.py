"""테스트 공통 설정.

Qt를 쓰는 테스트가 디스플레이 없는 환경(CI)에서도 돌도록 offscreen을 기본값으로
한다. 이미 지정돼 있으면 존중한다.
"""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
