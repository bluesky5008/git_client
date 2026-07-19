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

# **크기 부분이 선택적인 이유**: git은 전송이 충분히 크거나 오래 걸릴 때만
# 크기·처리량을 붙인다. 작은 clone은 `Receiving objects: 100% (3/3), done.`
# 처럼 개수만 준다(실측: 200KB까지는 크기 없음, 2MB부터 나옴).
#
# 크기를 필수로 두면 그런 경우에 **객체 수까지 통째로 놓친다** — 알 수 있는
# 것마저 "측정 못함"으로 버리는 셈이다. 개수는 받고, 크기는 없으면 None으로
# 둔다(진짜 측정 실패). 둘은 별개의 사실이다.
# **100%를 요구하지 않는다.** stall이나 취소로 끊긴 전송은 정의상 100% 이전에
# 죽는다. 100%를 필수로 두면 회선에 **이미 실린 바이트가 통째로 기록되지
# 않아**(billed_bytes=None) 누적 전송량이 조용히 과소 집계된다 — 하필 느리고
# 불안정한 회선에서만 일어나므로 집계표만 봐서는 탐지되지 않는다. ADR-26이
# 막으려던 바로 그 무기록이다.
#
# 진행률 줄은 CR로 덮어쓰이며 순서대로 오고 아래 루프가 마지막 매칭으로
# 덮어쓰므로, 성공 경로에서는 여전히 마지막 100% 줄의 값이 남는다.
_RECEIVING = re.compile(
    r"Receiving objects:\s+\d+%\s+\((\d+)/(\d+)\)"
    r"(?:,\s+([\d.]+)\s+" + _SIZE_UNIT +
    r"(?:\s*\|\s*([\d.]+)\s+" + _SIZE_UNIT + r"/s)?)?"
)

# push의 대응물. 형태는 같지만 방향이 반대다 — 우리가 **보낸** 바이트다.
# "Writing objects: 100% (3/3), 429 bytes | 429.00 KiB/s, done."
_WRITING = re.compile(
    r"Writing objects:\s+\d+%\s+\((\d+)/(\d+)\),\s+"
    r"([\d.]+)\s+" + _SIZE_UNIT +
    r"(?:\s*\|\s*([\d.]+)\s+" + _SIZE_UNIT + r"/s)?"
)

# "Everything up-to-date" — push가 할 일이 없을 때. ref 줄이 아예 없다.
_NOTHING_TO_PUSH = re.compile(r"^Everything up-to-date")

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
#
# 화살표를 선택적으로 둔다 — push의 삭제 줄에는 화살표가 없다:
#   fetch  " - [deleted]         (none)     -> origin/side"
#   push   " - [deleted]         feature"
# 필수로 두면 push 삭제가 통째로 안 잡혀 "0개 참조 갱신"으로 보고된다.
_REF_MARKER = re.compile(r"^([*t+-])\s+\[([^\]]+)\]\s+(\S+)(?:\s+->\s+(\S+))?")

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
    """원격 작업이 갱신한 참조 하나.

    **필드 이름이 방향 중립인 이유**: 화살표의 의미가 작업마다 뒤집힌다.

        fetch  " * [new branch]  side -> origin/side"   원격 브랜치 → 로컬 추적 ref
        push   " * [new branch]  main -> main"          로컬 브랜치 → 원격 브랜치

    즉 fetch에서 왼쪽은 원격의 것이고 push에서 왼쪽은 우리 것이다.
    `local_ref`/`remote_ref`로 이름 붙이면 한쪽에서 반드시 거짓말이 된다.
    """

    source: str
    """화살표 왼쪽. fetch면 원격 브랜치, push면 로컬 브랜치."""

    dest: str
    """화살표 오른쪽. fetch면 로컬 추적 ref, push면 원격 브랜치.

    화살표가 없는 줄(push의 삭제)에서는 source와 같다.
    """

    old_sha: str | None = None
    new_sha: str | None = None
    deleted: bool = False
    """지워진 참조. sha가 없다고 '신규'로 오인하면 안 된다."""

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
    sent_bytes: int | None = None
    """push가 보낸 바이트. 목적함수는 방향을 가리지 않으므로 함께 센다."""

    sent_objects: int | None = None
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
    def billed_bytes(self) -> int | None:
        """이 작업이 회선에 실은 총 바이트 — 목적함수의 단위.

        받은 것과 보낸 것을 합친다. 트래픽 예산은 방향을 가리지 않으므로,
        누적 집계는 이 값으로 해야 한다. 한쪽이라도 측정하지 못했으면
        합계도 "측정하지 못함"이다 — 0으로 때우면 과소 집계가 된다.
        """
        if self.received_bytes is None and self.sent_bytes is None:
            return None
        if self.received_bytes is None or self.sent_bytes is None:
            # 한 방향만 측정된 경우. fetch는 sent를, push는 received를
            # 애초에 만들지 않으므로, 없는 쪽은 0으로 본다.
            return self.received_bytes if self.sent_bytes is None else self.sent_bytes
        return self.received_bytes + self.sent_bytes

    @property
    def transferred_anything(self) -> bool:
        """팩을 실제로 주고받았는가.

        **이 값을 "변경 없음"으로 읽으면 안 된다.** 객체를 하나도 옮기지 않고
        참조만 바뀌는 작업이 있다 — 이미 가진 커밋을 가리키는 새 브랜치,
        prune 삭제, 원격에 이미 있는 커밋을 가리키는 브랜치 push.
        그 판단은 `changed_anything`이다.
        """
        return bool(self.received_objects) or bool(self.sent_objects)

    @property
    def changed_anything(self) -> bool:
        """저장소(또는 원격) 상태가 바뀌었는가 — 화면을 다시 그려야 하는가.

        `transferred_anything`으로 이걸 판단하면 .git과 화면이 어긋난다.
        실측: 원격에 새 브랜치가 생겼는데 그 커밋을 이미 갖고 있으면 팩이
        오지 않아 "이미 최신"으로 표시되고, 참조 목록에 브랜치가 끝내
        나타나지 않는다.
        """
        return self.transferred_anything or bool(self.ref_updates)

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
    sent_bytes: int | None = None
    """push가 보낸 바이트. "Writing objects" 줄에서 온다."""

    sent_objects: int | None = None
    total_objects: int | None = None
    reused_objects: int | None = None
    delta_objects: int | None = None
    throughput_bytes_per_s: int | None = None
    ref_updates: list[RefUpdate] = field(default_factory=list)
    nothing_to_push: bool = False
    """"Everything up-to-date" — push할 것이 없었다."""



class TransferPhase(Enum):
    """git이 지나가는 진행 단계.

    **단계마다 기다림의 성격이 다르다.** 원격이 준비하는 동안은 회선이 놀고
    있고, 받는 동안은 회선이 병목이며, 적용하는 동안은 로컬 CPU·디스크가
    병목이다. 사용자가 "왜 이렇게 오래 걸리나"를 판단하려면 구분이 필요하다.
    """

    PREPARING = "preparing"
    """객체를 세고 압축하는 중 (Enumerating/Counting/Compressing).

    회선이 아니라 CPU가 병목인 구간이다. **누구의 CPU인지는 `remote_side`가
    정한다** — fetch에서는 서버가 세지만 push에서는 우리가 센다.
    """

    RECEIVING = "receiving"
    """실제로 받는 중. **느린 회선에서 기다림의 대부분이 여기다.**

    유일하게 바이트와 속도가 함께 오는 단계다.
    """

    SENDING = "sending"
    """실제로 보내는 중 (push의 Writing objects)."""

    APPLYING = "applying"
    """팩을 풀어 반영하는 중 (Resolving deltas / Updating files).

    회선과 무관하다. **다만 누구의 CPU인지는 `remote_side`가 정한다** —
    fetch에서는 우리 쪽이지만 push에서는 서버 쪽이다.
    """


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """지금 이 순간의 진행 상태. 화면에 그리기 위한 값이다.

    `ProgressReport`와 목적이 다르다 — 그쪽은 작업이 끝난 뒤의 **회계**이고,
    이쪽은 진행 중의 **표시**다. 섞으면 "끝나야 알 수 있는 값"과 "지금
    보여줘야 하는 값"이 한 타입에 뒤엉킨다.
    """

    phase: TransferPhase
    remote_side: bool = False
    """이 일을 **누가** 하고 있는가 — 서버(True)인가 우리(False)인가.

    `remote:` 접두어가 유일한 판별 정보다. 단계 이름만으로는 알 수 없고,
    **fetch와 push에서 정반대로 붙는다**:
      fetch: `remote: Counting` (서버가 센다) / `Resolving deltas` (우리가 푼다)
      push:  `Counting` (우리가 센다) / `remote: Resolving deltas` (서버가 푼다)
    버리면 push에서 귀속이 통째로 뒤집혀, 사용자의 CPU가 돌 때 서버를
    지목하고 서버가 몇 분 걸릴 때 사용자 디스크를 지목한다.
    """

    percent: int | None = None
    current: int | None = None
    total: int | None = None
    bytes_so_far: int | None = None
    bytes_per_s: int | None = None

    # **남은 시간은 표시하지 않는다.**
    #
    # 처음에는 `받은 바이트 ÷ 진행률`로 역산해 20% 이상에서만 보여줬다.
    # 그 추정은 "객체당 바이트가 전송 내내 일정하다"를 가정하는데, 팩은
    # 커밋 → 트리 → blob 순으로 쓰이고 큰 것은 대개 blob이다. 즉 객체당
    # 바이트가 **뒤로 갈수록 커진다** — 초반 추정은 구조적으로 낙관적이다.
    # 임계값을 올려도 편향이 사라지지 않고 틀린 값이 늦게 나올 뿐이다.
    #
    # git 자신도 속도만 보여주고 남은 시간은 말하지 않는다. 총 바이트를
    # 모르기 때문이다. 우리도 모른다. 대신 속도와 받은 양을 그대로 보여주고
    # 남은 시간은 사용자가 판단하게 둔다. (§4.6.5, ADR-59)


# 진행률 줄 하나를 통째로 읽는다. `remote:` 접두어는 서버측 단계를 뜻한다.
_SNAPSHOT = re.compile(
    r"(remote:\s*)?"
    r"(Enumerating objects|Counting objects|Compressing objects|"
    r"Receiving objects|Writing objects|Resolving deltas|Updating files)"
    r":\s+(?:(\d+)%\s+\((\d+)/(\d+)\)|(\d+))"
    r"(?:,\s+([\d.]+)\s+" + _SIZE_UNIT +
    r"(?:\s*\|\s*([\d.]+)\s+" + _SIZE_UNIT + r"/s)?)?"
)

_PHASE_BY_LABEL = {
    "Enumerating objects": TransferPhase.PREPARING,
    "Counting objects": TransferPhase.PREPARING,
    "Compressing objects": TransferPhase.PREPARING,
    "Receiving objects": TransferPhase.RECEIVING,
    "Writing objects": TransferPhase.SENDING,
    "Resolving deltas": TransferPhase.APPLYING,
    "Updating files": TransferPhase.APPLYING,
}


def parse_progress_snapshot(stderr: str) -> ProgressSnapshot | None:
    """지금까지의 stderr에서 **가장 최근** 진행 상태를 뽑는다.

    진행률은 캐리지 리턴으로 덮어쓰이며 오므로 마지막 매칭이 현재 상태다.
    아직 어떤 단계도 시작되지 않았으면 None.
    """
    latest = None
    for match in _SNAPSHOT.finditer(stderr):
        latest = match
    if latest is None:
        return None

    (remote_prefix, label, percent, current, total, bare_count,
     size, size_unit, speed, speed_unit) = latest.groups()

    return ProgressSnapshot(
        phase=_PHASE_BY_LABEL[label],
        remote_side=bool(remote_prefix),
        percent=int(percent) if percent else None,
        # "Enumerating objects: 13" 처럼 비율 없이 개수만 오는 단계가 있다.
        current=int(current) if current else (
            int(bare_count) if bare_count else None
        ),
        total=int(total) if total else None,
        bytes_so_far=parse_size(size, size_unit) if size else None,
        bytes_per_s=parse_size(speed, speed_unit) if speed else None,
    )

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
            if match.group(3) and match.group(4):
                report.received_bytes = parse_size(match.group(3), match.group(4))
            if match.group(5) and match.group(6):
                report.throughput_bytes_per_s = parse_size(
                    match.group(5), match.group(6)
                )
            continue

        match = _WRITING.search(line)
        if match:
            report.sent_objects = int(match.group(1))
            report.sent_bytes = parse_size(match.group(3), match.group(4))
            if match.group(5) and match.group(6):
                report.throughput_bytes_per_s = parse_size(
                    match.group(5), match.group(6)
                )
            continue

        if _NOTHING_TO_PUSH.search(line):
            report.nothing_to_push = True
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
                    source=match.group(3),
                    dest=match.group(4),
                    old_sha=match.group(1),
                    new_sha=match.group(2),
                )
            )
            continue

        match = _REF_MARKER.search(line)
        if match:
            source = match.group(3)
            # 화살표가 없으면(push 삭제) 대상은 source 자신이다.
            report.ref_updates.append(
                RefUpdate(
                    source=source,
                    dest=match.group(4) or source,
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
