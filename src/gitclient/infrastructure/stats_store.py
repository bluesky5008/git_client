"""원격 작업 계측 이력 저장소 (SQLite).

ADR-12가 "SQLite는 캐시 전용, Phase 3에 도입"으로 예고한 그 시점이다.
설정은 QSettings가 맡고, 여기는 **누적 집계와 질의가 필요한 계측 데이터**만
다룬다 — 롤링 보관과 기간별 합산이 필요해 파일 한 장으로는 부족하기 때문이다.
(doc/design.md §4.5)

저장 위치는 사용자 캐시 디렉터리다. 저장소 안(.git)에 두면 clone·삭제로
이력이 사라지고, 저장소를 지워도 "이 원격에서 얼마나 받았나"는 남는 편이 낫다.

**계측 실패가 본 작업을 실패시키지 않는다.** 저장에 실패해도 fetch는 성공한
것이며, 조용히 넘어가되 로그는 남긴다.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from gitclient.domain.instrumentation import TransferStats

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS remote_stats (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at           TEXT    NOT NULL,
    repo_key              TEXT    NOT NULL,
    remote                TEXT    NOT NULL,
    kind                  TEXT    NOT NULL,
    succeeded             INTEGER NOT NULL,
    duration_ms           INTEGER NOT NULL,
    received_bytes        INTEGER,
    received_objects      INTEGER,
    total_objects         INTEGER,
    negotiation_rounds    INTEGER,
    protocol_version      INTEGER
);

CREATE INDEX IF NOT EXISTS idx_remote_stats_repo_time
    ON remote_stats (repo_key, recorded_at);
"""

# v1 → v2: push가 등장하면서 방향이 생겼다. 받은 바이트만으로는 목적함수를
# 셀 수 없다. 기존 행은 전부 fetch라 sent_* 가 NULL인 채로 맞다.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE remote_stats ADD COLUMN sent_bytes INTEGER",
        "ALTER TABLE remote_stats ADD COLUMN sent_objects INTEGER",
    ),
}


@dataclass(frozen=True, slots=True)
class TransferSummary:
    """기간별 집계.

    `measured_operations`가 `operations`보다 작으면 일부 작업의 전송량을
    측정하지 못했다는 뜻이다 — 합계를 "전부"라고 읽으면 안 된다.
    """

    operations: int = 0
    measured_operations: int = 0
    total_bytes: int = 0
    total_objects: int = 0
    total_duration_ms: int = 0

    @property
    def fully_measured(self) -> bool:
        return self.operations == self.measured_operations


class StatsStore:
    """계측 이력의 append-only 로그.

    롤링 보관: 저장소별 최근 N건만 남긴다. 계측은 추세 판단용이라
    전체 이력이 필요하지 않고, 무한히 쌓이면 캐시 파일이 커진다.
    """

    def __init__(self, db_path: str | Path, *, keep_per_repo: int = 500) -> None:
        self._path = Path(db_path)
        self._keep = keep_per_repo
        self._ensure_schema()

    @classmethod
    def default_path(cls) -> Path:
        """사용자 캐시 디렉터리 안의 기본 위치."""
        from PySide6.QtCore import QStandardPaths

        base = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.AppDataLocation
        )
        return Path(base or ".") / "stats.sqlite3"

    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        try:
            with closing(self._connect()) as connection:
                connection.executescript(_SCHEMA)
                row = connection.execute(
                    "SELECT version FROM schema_info LIMIT 1"
                ).fetchone()
                if row is None:
                    # 새 파일. _SCHEMA는 v1 형태이므로 마이그레이션을 태워
                    # 최신으로 올린다 — 신규와 기존의 스키마가 갈라지면 안 된다.
                    self._migrate(connection, 1)
                    connection.execute(
                        "INSERT INTO schema_info (version) VALUES (?)",
                        (SCHEMA_VERSION,),
                    )
                elif int(row["version"]) < SCHEMA_VERSION:
                    self._migrate(connection, int(row["version"]))
                    connection.execute(
                        "UPDATE schema_info SET version = ?", (SCHEMA_VERSION,)
                    )
                connection.commit()
        except sqlite3.Error as exc:
            logger.warning("계측 저장소를 준비하지 못했습니다: %s", exc)

    def _migrate(self, connection: sqlite3.Connection, from_version: int) -> None:
        """계측은 캐시다 — 마이그레이션이 실패하면 버리고 다시 쌓아도 된다.

        다만 조용히 버리지는 않는다. 누적 전송량 추세가 끊기는 것은
        사용자에게 보이는 손실이므로 로그를 남긴다.
        """
        for version in range(from_version + 1, SCHEMA_VERSION + 1):
            for statement in _MIGRATIONS.get(version, ()):
                try:
                    connection.execute(statement)
                except sqlite3.OperationalError as exc:
                    # 이미 있는 열이면 정상 — 스키마 생성과 겹칠 수 있다.
                    if "duplicate column" not in str(exc).lower():
                        raise

    def record(self, repo_key: str, stats: TransferStats) -> None:
        """계측 한 건을 남긴다. 실패해도 예외를 던지지 않는다."""
        try:
            with closing(self._connect()) as connection:
                connection.execute(
                    """
                    INSERT INTO remote_stats (
                        recorded_at, repo_key, remote, kind, succeeded,
                        duration_ms, received_bytes, received_objects,
                        sent_bytes, sent_objects,
                        total_objects, negotiation_rounds, protocol_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        datetime.now(timezone.utc).isoformat(),
                        repo_key,
                        stats.remote,
                        stats.kind.value,
                        int(stats.succeeded),
                        stats.duration_ms,
                        stats.received_bytes,
                        stats.received_objects,
                        stats.sent_bytes,
                        stats.sent_objects,
                        stats.total_objects,
                        stats.negotiation_rounds,
                        stats.protocol_version,
                    ),
                )
                self._trim(connection, repo_key)
                connection.commit()
        except sqlite3.Error as exc:
            # 계측은 부가 기능이다 — 저장 실패로 fetch를 실패시키지 않는다.
            logger.warning("계측을 저장하지 못했습니다: %s", exc)

    def _trim(self, connection: sqlite3.Connection, repo_key: str) -> None:
        """저장소별로 최근 기록만 남긴다 — **종류별로 따로 센다.**

        하나의 링에 섞어 담으면 배경 prefetch가 5분마다 행을 밀어 넣어
        사용자 작업 기록을 밀어낸다. 사용자가 실제로 기다린 시간이 배경
        작업에 덮이는 셈이라, 대기 시간이 목적 함수인 이 앱에서 가장 중요한
        기록이 먼저 사라진다.
        """
        for (kind,) in connection.execute(
            "SELECT DISTINCT kind FROM remote_stats WHERE repo_key = ?",
            (repo_key,),
        ).fetchall():
            connection.execute(
                """
                DELETE FROM remote_stats
                 WHERE repo_key = ?
                   AND kind = ?
                   AND id NOT IN (
                       SELECT id FROM remote_stats
                        WHERE repo_key = ?
                          AND kind = ?
                        ORDER BY id DESC
                        LIMIT ?
                   )
                """,
                (repo_key, kind, repo_key, kind, self._keep),
            )

    def summarize(self, repo_key: str, *, since: datetime | None = None) -> TransferSummary:
        """기간 집계. `since`를 주면 그 시각 이후만 센다."""
        # 목적함수는 방향을 가리지 않는다 — 받은 것과 보낸 것을 함께 센다.
        # "측정됨"의 판정도 둘 중 **해당 방향**이 채워졌는지로 봐야 한다.
        # received만 보면 push 행이 전부 미측정으로 잡혀 신호가 무의미해진다.
        query = [
            "SELECT COUNT(*) AS ops,",
            "       SUM(received_bytes IS NOT NULL",
            "           OR sent_bytes IS NOT NULL) AS measured,",
            "       COALESCE(SUM(COALESCE(received_bytes, 0)",
            "                    + COALESCE(sent_bytes, 0)), 0) AS bytes,",
            "       COALESCE(SUM(COALESCE(received_objects, 0)",
            "                    + COALESCE(sent_objects, 0)), 0) AS objects,",
            "       COALESCE(SUM(duration_ms), 0) AS duration",
            "  FROM remote_stats WHERE repo_key = ?",
        ]
        params: list[object] = [repo_key]
        if since is not None:
            query.append("AND recorded_at >= ?")
            params.append(since.isoformat())

        try:
            with closing(self._connect()) as connection:
                row = connection.execute(" ".join(query), params).fetchone()
        except sqlite3.Error as exc:
            logger.warning("계측을 읽지 못했습니다: %s", exc)
            return TransferSummary()

        if row is None or not row["ops"]:
            return TransferSummary()

        return TransferSummary(
            operations=int(row["ops"]),
            measured_operations=int(row["measured"] or 0),
            total_bytes=int(row["bytes"]),
            total_objects=int(row["objects"]),
            total_duration_ms=int(row["duration"]),
        )

    def recent(self, repo_key: str, limit: int = 20) -> list[sqlite3.Row]:
        try:
            with closing(self._connect()) as connection:
                return list(
                    connection.execute(
                        """
                        SELECT * FROM remote_stats
                         WHERE repo_key = ?
                         ORDER BY id DESC LIMIT ?
                        """,
                        (repo_key, limit),
                    )
                )
        except sqlite3.Error as exc:
            logger.warning("계측을 읽지 못했습니다: %s", exc)
            return []
