"""원격 작업 계측 — 전송량과 구간별 소요시간.

이 프로젝트의 목적함수는 **누적 전송 바이트**다(performance.md §1.1).
따라서 최적화를 주장하려면 먼저 측정할 수 있어야 한다. Phase 3의 첫 작업이
기능이 아니라 계측 체계인 이유다. (doc/design.md §10)

이 모듈은 순수 파이썬이다 — git을 실행하지 않고 그 **출력만** 해석한다.
덕분에 까다로운 파싱(단위 환산, 진행률 덮어쓰기, 로케일)을 git 없이
단위 테스트로 고정할 수 있다.

**측정원 두 가지**

  stderr progress   전송 바이트, 객체 수, 처리량
  Trace2 이벤트     구간별 소요시간, 협상 왕복 횟수, 협상된 프로토콜 버전

`Receiving objects` 줄은 git이 받은 팩을 **풀지 않고 보관할 때만** 나온다.
기본값(`transfer.unpackLimit=100`)에서는 작은 fetch가 통째로 풀려 이 줄이
사라지므로, 계측 경로는 `transfer.unpackLimit=1`을 명령 단위로 강제한다.
부수 효과로 저장소가 팩된 상태를 유지하는데, 이는 §4.1.1.3에서 측정한
"팩된 저장소가 40배 빠름"과 같은 방향이라 이득이 겹친다.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum

# "Receiving objects: 100% (24/24), 1.93 KiB | 1.93 MiB/s, done."
# 작은 전송은 "773 bytes | 773.00 KiB/s" 처럼 소문자 복수형으로 온다 —
# 실제 fetch에서 확인된 형태이며, 이걸 놓치면 작은 fetch가 통째로
# "측정 실패"로 기록되어 누적 전송량이 과소 집계된다.
_SIZE_UNIT = r"(bytes|byte|[KMGT]iB|[KMGT]B|B)"
_RECEIVING = re.compile(
    r"Receiving objects:\s+100%\s+\((\d+)/(\d+)\),\s+"
    r"([\d.]+)\s+" + _SIZE_UNIT +
    r"(?:\s*\|\s*([\d.]+)\s+" + _SIZE_UNIT + r"/s)?"
)

# "remote: Total 24 (delta 12), reused 0 (delta 0), pack-reused 0"
_TOTAL = re.compile(
    r"Total\s+(\d+)\s+\(delta\s+(\d+)\),\s+reused\s+(\d+)\s+\(delta\s+(\d+)\)"
)

# fetch가 내는 ref 줄. 첫 글자가 상태 플래그다 (git builtin/fetch.c format_display):
#   ' ' 빨리 감기 | '+' 강제 갱신 | '*' 신규 | '-' prune 삭제 | 't' 태그 갱신
#   '=' 최신     | '!' 거부      <- 갱신이 아니므로 세지 않는다
#
# 빨리 감기는 "a..b", 강제 갱신은 "a...b"로 **점 개수가 다르다.** 점 두 개만
# 받으면 force-push된 원격에서 참조 갱신이 통째로 0건으로 보고된다.
_REF_UPDATE = re.compile(
    r"^\+?\s*([0-9a-f]{7,40})\.\.\.?([0-9a-f]{7,40})\s+(\S+)\s+->\s+(\S+)"
)
# "* [new branch]", "* [new tag]", "* [new ref]", "t [tag update]", "- [deleted]".
# 대괄호 안을 열어두는 이유: git이 새 표식을 추가해도 갱신 수가 조용히 틀리지 않게.
_REF_MARKER = re.compile(r"^([*t+-])\s+\[([^\]]+)\]\s+(\S+)\s+->\s+(\S+)")

_UNIT_BYTES = {
    "B": 1,
    "byte": 1,
    "bytes": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
    # git은 이진 접두사를 쓰지만 방어적으로 십진도 받아둔다
    "KB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
}


class OperationKind(Enum):
    CLONE = "clone"
    FETCH = "fetch"
    PULL = "pull"
    PUSH = "push"


def parse_size(value: str, unit: str) -> int:
    """"1.93", "KiB" → 바이트 정수.

    git이 소수점 두 자리로 반올림해 보고하므로 복원값도 근사다.
    바이트 단위로 저장하는 이유는 단위가 섞인 값을 나중에 합산할 수 없기 때문이다.
    """
    multiplier = _UNIT_BYTES.get(unit)
    if multiplier is None:
        raise ValueError(f"알 수 없는 크기 단위입니다: {unit!r}")
    return int(round(float(value) * multiplier))


@dataclass(frozen=True, slots=True)
class RefUpdate:
    """fetch가 갱신한 참조 하나."""

    local_ref: str
    remote_ref: str
    old_sha: str | None = None
    new_sha: str | None = None
    deleted: bool = False
    """prune이 지운 참조. sha가 없다고 '신규'로 오인하면 안 된다."""

    @property
    def is_new(self) -> bool:
        return self.old_sha is None and not self.deleted


@dataclass(frozen=True, slots=True)
class TransferStats:
    """원격 작업 한 번의 계측 결과.

    `received_bytes`가 None이면 "측정하지 못함"이지 0이 아니다 — 둘을 섞으면
    누적 전송량이 조용히 과소 집계된다.
    """

    kind: OperationKind
    remote: str
    duration_ms: int
    received_bytes: int | None = None
    received_objects: int | None = None
    total_objects: int | None = None
    reused_objects: int | None = None
    throughput_bytes_per_s: int | None = None
    negotiation_rounds: int | None = None
    protocol_version: int | None = None
    ref_updates: tuple[RefUpdate, ...] = ()
    regions: tuple[tuple[str, float], ...] = ()
    """(구간 이름, 소요 초). Trace2의 region_leave에서 얻는다."""

    succeeded: bool = True

    @property
    def transferred_anything(self) -> bool:
        """팩을 실제로 받았는가.

        **이 값을 "변경 없음"으로 읽으면 안 된다.** 객체를 하나도 받지 않고
        참조만 바뀌는 fetch가 있다 — 이미 가진 커밋을 가리키는 새 브랜치,
        prune 삭제. 그 판단은 `changed_anything`이다.
        """
        return bool(self.received_objects)

    @property
    def changed_anything(self) -> bool:
        """저장소 상태가 바뀌었는가 — 화면을 다시 그려야 하는가.

        `transferred_anything`으로 이걸 판단하면 .git과 화면이 어긋난다.
        실측: 원격에 새 브랜치가 생겼는데 그 커밋을 이미 갖고 있으면 팩이
        오지 않아 "이미 최신"으로 표시되고, 참조 목록에 브랜치가 끝내
        나타나지 않는다.
        """
        return bool(self.received_objects) or bool(self.ref_updates)

    def region_ms(self, name: str) -> float | None:
        for label, seconds in self.regions:
            if label == name:
                return seconds * 1000
        return None


@dataclass
class ProgressReport:
    """stderr progress에서 뽑아낸 값들."""

    received_bytes: int | None = None
    received_objects: int | None = None
    total_objects: int | None = None
    reused_objects: int | None = None
    delta_objects: int | None = None
    throughput_bytes_per_s: int | None = None
    ref_updates: list[RefUpdate] = field(default_factory=list)


def parse_progress(stderr: str) -> ProgressReport:
    """git의 stderr progress를 해석한다.

    진행률 줄은 캐리지 리턴으로 덮어쓰이며 오므로 줄 분리 시 `\\r`도 경계로
    다뤄야 한다. 그러지 않으면 한 줄에 진행률이 수십 개 붙어 정규식이
    마지막 값을 놓친다.
    """
    report = ProgressReport()

    for raw in re.split(r"[\r\n]+", stderr):
        line = raw.strip()
        if not line:
            continue

        match = _RECEIVING.search(line)
        if match:
            report.received_objects = int(match.group(1))
            report.received_bytes = parse_size(match.group(3), match.group(4))
            if match.group(5) and match.group(6):
                report.throughput_bytes_per_s = parse_size(
                    match.group(5), match.group(6)
                )
            continue

        match = _TOTAL.search(line)
        if match:
            report.total_objects = int(match.group(1))
            report.delta_objects = int(match.group(2))
            report.reused_objects = int(match.group(3))
            continue

        match = _REF_UPDATE.search(line)
        if match:
            report.ref_updates.append(
                RefUpdate(
                    local_ref=match.group(4),
                    remote_ref=match.group(3),
                    old_sha=match.group(1),
                    new_sha=match.group(2),
                )
            )
            continue

        match = _REF_MARKER.search(line)
        if match:
            report.ref_updates.append(
                RefUpdate(
                    local_ref=match.group(4),
                    remote_ref=match.group(3),
                    deleted=match.group(1) == "-",
                )
            )

    return report


@dataclass
class TraceReport:
    """Trace2 이벤트 스트림에서 뽑아낸 값들."""

    negotiation_rounds: int | None = None
    protocol_version: int | None = None
    regions: list[tuple[str, float]] = field(default_factory=list)


# 관심 있는 구간만 남긴다 — Trace2는 수백 개를 쏟아내므로 전부 저장하면
# 저장소가 잡음으로 찬다. 여기 없는 구간은 진단이 필요할 때 원본을 다시 뜬다.
INTERESTING_REGIONS = frozenset(
    {
        ("fetch", "remote_refs"),      # ref 광고 + 연결 수립
        ("fetch", "fetch_refs"),       # 실제 객체 전송
        ("fetch-pack", "negotiation_v2"),
        ("pack-objects", "write-pack-file"),
        ("index-pack", "unpack"),
    }
)


def parse_trace2(trace_text: str) -> TraceReport:
    """Trace2 이벤트(JSON Lines)를 해석한다.

    깨진 줄은 조용히 건너뛴다 — 계측은 부가 기능이므로 파싱 실패가
    본 작업을 실패시켜서는 안 된다.
    """
    report = TraceReport()

    for line in trace_text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue

        kind = event.get("event")

        if kind in ("data", "data_json"):
            category, key = event.get("category"), event.get("key")
            value = event.get("value")
            if category == "transfer" and key == "negotiated-version":
                report.protocol_version = _as_int(value)
            elif category == "negotiation_v2" and key == "total_rounds":
                report.negotiation_rounds = _as_int(value)

        elif kind == "region_leave":
            category, label = event.get("category"), event.get("label")
            elapsed = event.get("t_rel")
            if (category, label) in INTERESTING_REGIONS and isinstance(
                elapsed, (int, float)
            ):
                report.regions.append((f"{category}.{label}", float(elapsed)))

    return report


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
