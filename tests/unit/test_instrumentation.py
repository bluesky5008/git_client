"""계측 파서 단위 테스트.

git 실행 없이 출력 해석만 검증한다. 실제 git이 이런 출력을 내는지는
tests/integration/test_remote_engine.py가 확인한다.
"""

from __future__ import annotations

import json

import pytest

from gitclient.domain.instrumentation import (
    TransferPhase,
    parse_progress_snapshot,
    OperationKind,
    TransferStats,
    parse_progress,
    parse_size,
    parse_trace2,
)


class TestParseSize:
    def test_kib(self) -> None:
        assert parse_size("1.93", "KiB") == 1976  # 1.93 * 1024 = 1976.32

    def test_mib(self) -> None:
        assert parse_size("12.5", "MiB") == 13107200

    def test_plain_bytes(self) -> None:
        assert parse_size("512", "B") == 512

    def test_lowercase_plural_bytes(self) -> None:
        """git은 작은 전송을 "773 bytes" 로 보고한다 — 실제 fetch에서 확인된 형태."""
        assert parse_size("773", "bytes") == 773

    def test_unknown_unit_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            parse_size("1", "furlongs")


class TestParseProgress:
    RECEIVING = (
        "remote: Enumerating objects: 25, done.\n"
        "remote: Counting objects: 100% (25/25), done.\n"
        "remote: Compressing objects: 100% (24/24), done.\n"
        "remote: Total 24 (delta 12), reused 3 (delta 1), pack-reused 0\n"
        "Receiving objects: 100% (24/24), 1.93 KiB | 1.93 MiB/s, done.\n"
        "From file:///tmp/origin\n"
        "   5a3dcca..db339f4  main       -> origin/main\n"
    )

    def test_received_bytes(self) -> None:
        assert parse_progress(self.RECEIVING).received_bytes == 1976

    def test_received_objects(self) -> None:
        assert parse_progress(self.RECEIVING).received_objects == 24

    def test_throughput(self) -> None:
        report = parse_progress(self.RECEIVING)
        assert report.throughput_bytes_per_s == round(1024**2 * 1.93)

    def test_total_and_reused(self) -> None:
        report = parse_progress(self.RECEIVING)
        assert report.total_objects == 24
        assert report.delta_objects == 12
        assert report.reused_objects == 3

    def test_ref_update(self) -> None:
        report = parse_progress(self.RECEIVING)
        assert len(report.ref_updates) == 1
        update = report.ref_updates[0]
        assert update.source == "main"
        assert update.dest == "origin/main"
        assert update.old_sha == "5a3dcca"
        assert update.is_new is False

    def test_new_branch(self) -> None:
        text = " * [new branch]      feature/x  -> origin/feature/x\n"
        report = parse_progress(text)
        assert len(report.ref_updates) == 1
        assert report.ref_updates[0].is_new is True
        assert report.ref_updates[0].dest == "origin/feature/x"

    def test_carriage_returns_are_line_boundaries(self) -> None:
        """진행률은 \\r로 덮어쓰여 온다 — 경계로 다루지 않으면 값을 놓친다."""
        text = (
            "Receiving objects:  50% (12/24)\r"
            "Receiving objects:  99% (23/24)\r"
            "Receiving objects: 100% (24/24), 4.00 KiB | 1.00 MiB/s, done.\r\n"
        )
        assert parse_progress(text).received_bytes == 4096

    def test_small_transfer_uses_bytes_unit(self) -> None:
        """작은 fetch는 "773 bytes" 형태다. 놓치면 누적 전송량이 과소 집계된다."""
        text = "Receiving objects: 100% (9/9), 773 bytes | 773.00 KiB/s, done.\n"
        report = parse_progress(text)
        assert report.received_bytes == 773
        assert report.received_objects == 9
        assert report.throughput_bytes_per_s == round(773.00 * 1024)

    def test_no_transfer_yields_nothing(self) -> None:
        report = parse_progress("")
        assert report.received_bytes is None
        assert report.received_objects is None

    def test_unmeasured_is_none_not_zero(self) -> None:
        """측정 실패와 0바이트는 다르다 — 섞으면 누적 집계가 조용히 어긋난다."""
        report = parse_progress("remote: Total 5 (delta 2), reused 0 (delta 0)\n")
        assert report.total_objects == 5
        assert report.received_bytes is None


class TestRefUpdateForms:
    """git이 실제로 내는 ref 줄 형태들.

    전부 실제 fetch stderr에서 복사한 문자열이다. 처음 구현은 빨리 감기
    ("a..b")와 신규 브랜치만 알아서, force-push된 원격에서 참조 갱신이
    통째로 0건으로 보고됐다.
    """

    def test_forced_update_has_three_dots(self) -> None:
        """강제 갱신은 "a...b"다 — 점 두 개만 받으면 통째로 놓친다."""
        text = " + f21f548...5751dba main       -> origin/main  (forced update)\n"
        report = parse_progress(text)
        assert len(report.ref_updates) == 1
        update = report.ref_updates[0]
        assert update.dest == "origin/main"
        assert update.old_sha == "f21f548"
        assert update.new_sha == "5751dba"
        assert update.is_new is False

    def test_fast_forward_still_parses(self) -> None:
        text = "   5a3dcca..db339f4  main       -> origin/main\n"
        assert len(parse_progress(text).ref_updates) == 1

    def test_pruned_ref_is_parsed(self) -> None:
        """prune 삭제는 객체 전송이 없어 놓치기 쉽다 — 놓치면 화면에 유령이 남는다."""
        text = " - [deleted]         (none)     -> origin/side\n"
        report = parse_progress(text)
        assert len(report.ref_updates) == 1
        update = report.ref_updates[0]
        assert update.dest == "origin/side"
        assert update.deleted is True
        assert update.is_new is False  # sha가 없다고 '신규'가 아니다

    def test_new_ref_marker(self) -> None:
        """[new branch]/[new tag] 외에 [new ref]도 온다."""
        text = " * [new ref]         refs/x     -> refs/x\n"
        assert len(parse_progress(text).ref_updates) == 1

    def test_tag_update_marker(self) -> None:
        text = " t [tag update]      v1         -> v1\n"
        assert len(parse_progress(text).ref_updates) == 1

    def test_up_to_date_is_not_an_update(self) -> None:
        """'='는 갱신이 아니다 — 세면 변경 없는 fetch가 변경으로 보고된다."""
        text = " = [up to date]      main       -> origin/main\n"
        assert parse_progress(text).ref_updates == []

    def test_rejected_is_not_an_update(self) -> None:
        text = " ! [rejected]        v1         -> v1  (would clobber existing tag)\n"
        assert parse_progress(text).ref_updates == []


class TestParseTrace2:
    def build(self, events: list[dict]) -> str:
        return "\n".join(json.dumps(e) for e in events)

    def test_protocol_version(self) -> None:
        text = self.build(
            [{"event": "data", "category": "transfer",
              "key": "negotiated-version", "value": 2}]
        )
        assert parse_trace2(text).protocol_version == 2

    def test_negotiation_rounds(self) -> None:
        text = self.build(
            [{"event": "data", "category": "negotiation_v2",
              "key": "total_rounds", "value": 3}]
        )
        assert parse_trace2(text).negotiation_rounds == 3

    def test_interesting_regions_are_kept(self) -> None:
        text = self.build(
            [
                {"event": "region_leave", "category": "fetch",
                 "label": "remote_refs", "t_rel": 0.0777},
                {"event": "region_leave", "category": "fetch",
                 "label": "fetch_refs", "t_rel": 0.25},
            ]
        )
        regions = dict(parse_trace2(text).regions)
        assert regions["fetch.remote_refs"] == pytest.approx(0.0777)
        assert regions["fetch.fetch_refs"] == pytest.approx(0.25)

    def test_uninteresting_regions_are_dropped(self) -> None:
        """Trace2는 수백 개를 쏟아낸다 — 전부 저장하면 잡음이 된다."""
        text = self.build(
            [{"event": "region_leave", "category": "index",
              "label": "do_read_index", "t_rel": 0.0002}]
        )
        assert parse_trace2(text).regions == []

    def test_malformed_lines_are_skipped(self) -> None:
        """계측 실패가 본 작업을 실패시켜서는 안 된다."""
        text = (
            "not json at all\n"
            '{"event": "data", "category": "transfer", '
            '"key": "negotiated-version", "value": 2}\n'
            "{broken\n"
        )
        assert parse_trace2(text).protocol_version == 2

    def test_empty_trace(self) -> None:
        report = parse_trace2("")
        assert report.protocol_version is None
        assert report.regions == []


class TestTransferStats:
    def test_region_lookup(self) -> None:
        stats = TransferStats(
            kind=OperationKind.FETCH,
            remote="origin",
            duration_ms=120,
            regions=(("fetch.remote_refs", 0.05),),
        )
        assert stats.region_ms("fetch.remote_refs") == pytest.approx(50.0)
        assert stats.region_ms("nope") is None

    def test_transferred_anything(self) -> None:
        empty = TransferStats(OperationKind.FETCH, "origin", 10)
        assert empty.transferred_anything is False
        moved = TransferStats(
            OperationKind.FETCH, "origin", 10, received_objects=3
        )
        assert moved.transferred_anything is True

    def test_ref_only_change_transfers_nothing_but_still_changes(self) -> None:
        """팩 없이 참조만 바뀌는 fetch가 있다.

        원격의 새 브랜치가 이미 가진 커밋을 가리키면 객체 전송이 0인데
        참조는 늘어난다. 이걸 "변경 없음"으로 읽으면 .git에 있는 브랜치가
        화면에 끝내 나타나지 않는다.
        """
        report = parse_progress(
            "From /tmp/remote\n * [new branch]      side       -> origin/side\n"
        )
        stats = TransferStats(
            OperationKind.FETCH,
            "origin",
            10,
            received_objects=report.received_objects,
            ref_updates=tuple(report.ref_updates),
        )
        assert stats.transferred_anything is False  # 팩은 안 왔다
        assert stats.changed_anything is True  # 그래도 화면은 갱신해야 한다

    def test_pruned_ref_counts_as_change(self) -> None:
        report = parse_progress(
            " - [deleted]         (none)     -> origin/side\n"
        )
        stats = TransferStats(
            OperationKind.FETCH, "origin", 10, ref_updates=tuple(report.ref_updates)
        )
        assert stats.changed_anything is True

    def test_true_no_op_changes_nothing(self) -> None:
        assert TransferStats(OperationKind.FETCH, "origin", 10).changed_anything is False


class TestProgressSnapshot:
    """진행 중 화면에 그릴 값. 끝난 뒤의 회계(ProgressReport)와 목적이 다르다.

    아래 문자열은 전부 **실측한 것**이다 (git 2.42, 앱의 BASE_CONFIG 포함).
    지어낸 형식으로 테스트하면 파서는 통과하는데 화면은 비는 일이 생긴다.
    """

    def test_no_progress_yet_is_none(self) -> None:
        assert parse_progress_snapshot("") is None
        assert parse_progress_snapshot("From file:///tmp/x\n") is None

    def test_remote_side_phases_are_preparing(self) -> None:
        snapshot = parse_progress_snapshot("remote: Counting objects:  62% (10/16)\r")

        assert snapshot.phase is TransferPhase.PREPARING
        assert snapshot.percent == 62
        assert (snapshot.current, snapshot.total) == (10, 16)

    def test_enumerating_has_a_count_without_percent(self) -> None:
        """이 단계는 비율이 없다 — 총계를 아직 모르기 때문이다."""
        snapshot = parse_progress_snapshot("remote: Enumerating objects: 13, done.\r")

        assert snapshot.phase is TransferPhase.PREPARING
        assert snapshot.percent is None
        assert snapshot.current == 13

    def test_receiving_carries_bytes_and_speed(self) -> None:
        """느린 회선에서 기다림의 대부분이 이 단계다."""
        snapshot = parse_progress_snapshot(
            "Receiving objects:  47% (470/1000), 12.34 MiB | 1.20 MiB/s\r"
        )

        assert snapshot.phase is TransferPhase.RECEIVING
        assert snapshot.percent == 47
        assert snapshot.bytes_so_far == pytest.approx(12.34 * 1024**2, rel=0.01)
        assert snapshot.bytes_per_s == pytest.approx(1.20 * 1024**2, rel=0.01)

    def test_receiving_without_size_is_still_usable(self) -> None:
        """전송이 작으면 git이 크기를 생략한다 — 비율은 그래도 보여줘야 한다."""
        snapshot = parse_progress_snapshot("Receiving objects:   8% (1/12)\r")

        assert snapshot.phase is TransferPhase.RECEIVING
        assert snapshot.percent == 8
        assert snapshot.bytes_so_far is None

    def test_writing_is_sending(self) -> None:
        snapshot = parse_progress_snapshot(
            "Writing objects: 100% (12/12), 3.13 MiB | 18.94 MiB/s, done.\r"
        )

        assert snapshot.phase is TransferPhase.SENDING

    def test_resolving_deltas_is_applying(self) -> None:
        """회선과 무관한 구간 — 여기서 오래 걸리면 원인이 다르다."""
        snapshot = parse_progress_snapshot("Resolving deltas:  71% (5/7)\r")

        assert snapshot.phase is TransferPhase.APPLYING

    def test_latest_line_wins(self) -> None:
        """진행률은 CR로 덮어쓰이며 온다 — 마지막이 현재 상태다."""
        stream = (
            "remote: Counting objects: 100% (16/16)\r"
            "Receiving objects:  50% (6/12), 5.00 MiB | 1.00 MiB/s\r"
        )

        snapshot = parse_progress_snapshot(stream)

        assert snapshot.phase is TransferPhase.RECEIVING
        assert snapshot.percent == 50


class TestNoEtaIsPromised:
    """남은 시간을 표시하지 않기로 한 결정을 고정한다.

    역산의 가정("객체당 바이트가 일정하다")이 구조적으로 틀리기 때문이다 —
    팩은 커밋 → 트리 → blob 순으로 쓰이고 큰 것은 대개 blob이라, 객체당
    바이트가 뒤로 갈수록 커진다. 임계값을 올려도 편향은 남는다. (ADR-59)
    """

    def test_snapshot_does_not_expose_an_eta(self) -> None:
        snapshot = parse_progress_snapshot(
            "Receiving objects:  50% (6/12), 5.00 MiB | 1.00 MiB/s\r"
        )

        assert not hasattr(snapshot, "eta_seconds"), (
            "추정 근거가 구조적으로 편향돼 있어 표시하지 않기로 했다"
        )

    def test_speed_and_amount_are_still_reported(self) -> None:
        """남은 시간 대신 사실만 보여준다 — 판단은 사용자가 한다."""
        snapshot = parse_progress_snapshot(
            "Receiving objects:  50% (6/12), 5.00 MiB | 1.00 MiB/s\r"
        )

        assert snapshot.bytes_so_far and snapshot.bytes_per_s
