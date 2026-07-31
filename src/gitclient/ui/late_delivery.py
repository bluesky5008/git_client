"""파괴된 위젯으로의 늦은 워커 배달을 버리는 가드.

`cancel()`과 disconnect(`_detach_loaders`)로도 부족했다 — 이미 이벤트
루프에 **실린** 배달은 disconnect 뒤에도 도착한다. 수신자인 signals
객체는 파이썬이 붙들고 있어 살아 있고, 슬롯이 만지는 위젯·모델만 먼저
죽는다 (Windows CI 실측: 이전 테스트의 창이 지워진 뒤 그 창의 커밋
배치가 배달돼 `CommitGraphModel already deleted`). 배달의 표적이 이미
없다면 버리는 것 말고 옳은 처리가 없다.

main_window에 있던 것을 모듈로 올렸다 — 다이얼로그(파일 히스토리 등)도
같은 수명 문제를 가진다.
"""

from __future__ import annotations

import functools


def drops_late_deliveries(handler):  # noqa: ANN001, ANN201
    """C++ 쪽 위젯이 먼저 파괴된 뒤 도착한 워커 배달을 버린다."""

    @functools.wraps(handler)
    def guarded(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        import shiboken6

        if not shiboken6.isValid(self):
            return None
        return handler(self, *args, **kwargs)

    return guarded
