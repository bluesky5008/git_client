"""실행된 git 명령의 기록 (FR-11, §5.2 원칙 3).

GUI가 뒤에서 무엇을 하는지 보여주는 투명성 창구다 — 사용자가 같은 일을
터미널에서 재현하거나, 문제를 보고할 때 정확한 명령을 집을 수 있어야 한다.

**순수 파이썬이다** (§3.1 의존 방향). 기록은 워커 스레드에서 오므로
버퍼는 락으로 보호하고, UI 전달은 구독 콜백에 맡긴다 — Qt 시그널로
바꾸는 일은 표현 계층이 한다.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime

_MAX_RECORDS = 500

# URL 속 자격증명(userinfo). clone처럼 주소가 인자로 오는 명령에서
# `https://user:token@host/...`의 토큰이 화면에 남으면 안 된다 (FR-12).
_USERINFO = re.compile(r"(?<=://)[^/@\s]+@")


@dataclass(frozen=True, slots=True)
class CommandRecord:
    """git 명령 하나의 실행 흔적."""

    when: datetime
    argv: tuple[str, ...]
    """표시용으로 이미 가려진 인자들 — 원문이 아니다."""
    duration_ms: int
    returncode: int | None
    """None은 비정상 종료(타임아웃·실행 실패)다."""

    @property
    def succeeded(self) -> bool:
        return self.returncode == 0


def _masked(argument: str) -> str:
    return _USERINFO.sub("***@", argument)


class CommandLog:
    """프로세스 전역 링버퍼. 상한을 넘으면 오래된 것부터 버린다.

    구독자는 하나(로그 패널)를 전제한다 — 목록이 필요한 화면이 늘면
    그때 일반화한다 (YAGNI).
    """

    def __init__(self, limit: int = _MAX_RECORDS) -> None:
        self._records: deque[CommandRecord] = deque(maxlen=limit)
        self._lock = threading.Lock()
        self._listener = None

    def record(
        self,
        argv: tuple[str, ...] | list[str],
        *,
        duration_ms: int,
        returncode: int | None,
        when: datetime | None = None,
    ) -> CommandRecord:
        entry = CommandRecord(
            when=when if when is not None else datetime.now(),
            argv=tuple(_masked(a) for a in argv),
            duration_ms=duration_ms,
            returncode=returncode,
        )
        with self._lock:
            self._records.append(entry)
            listener = self._listener
        if listener is not None:
            try:
                listener(entry)
            except Exception:  # noqa: BLE001 - 표시 실패가 기록·작업을 막으면 안 된다
                pass
        return entry

    def snapshot(self) -> tuple[CommandRecord, ...]:
        with self._lock:
            return tuple(self._records)

    def subscribe(self, listener) -> None:  # noqa: ANN001
        """새 기록마다 부를 콜백. **워커 스레드에서 불린다** — 구독자가
        스레드 경계를 책임진다 (Qt라면 큐잉 시그널로)."""
        with self._lock:
            self._listener = listener


COMMAND_LOG = CommandLog()
"""프로세스 전역 인스턴스. 엔진들이 여기 기록하고 패널이 구독한다."""
