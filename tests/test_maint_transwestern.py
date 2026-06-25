"""Offline tests for the Transwestern maintenance source module.

Exercises the pure parse_* functions against committed fixtures (no network):
  * tests/fixtures/maint/transwestern_detail_118465.html  — the iPost detail page
  * tests/fixtures/maint/transwestern_notices_planned-service-outage.csv — the PSO CSV

Asserts the capacity transition (750,000 -> 650,000 MMBtu/d), the labelled fields,
the emitted NoticeEvent + MaintenanceImpact, and the orchestrator-overwritten
columns (is_current/has_capacity_impact/primary_point_id) per the contract.
"""
import pathlib

from etl.maintenance_sources import transwestern as tw

FIX = pathlib.Path(__file__).parent / "fixtures" / "maint"
PULLED = "2026-06-25T15:00:00Z"


def detail_html() -> str:
    return (FIX / "transwestern_detail_118465.html").read_text(encoding="utf-8")


def pso_csv() -> str:
    return (FIX / "transwestern_notices_planned-service-outage.csv").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Detail-page field parsing
# --------------------------------------------------------------------------- #


def test_parse_detail_labelled_fields():
    fields = tw.parse_detail(detail_html())
    assert fields["Notice Type Description"] == "Planned Service Outage"
    assert fields["Notice Identifier"] == "118465"
    assert fields["Notice Status Description"] == "Initiate"
    assert fields["Reason"] == "Pipeline Maintenance"
    assert fields["Location"] == "East Mainline System"
    assert fields["Subject"].startswith("East Mainline system - Compressor Station maintenance")
    # Notice Text label cell stacks two labels; keyed on the first line.
    assert "Notice Text" in fields
    assert "750,000 MMBtu/d to 650,000 MMBtu/d" in fields["Notice Text"]


# --------------------------------------------------------------------------- #
# Capacity-transition regex over the prose
# --------------------------------------------------------------------------- #


def test_extract_capacity_picks_the_cut_not_the_recovery():
    fields = tw.parse_detail(detail_html())
    cap = tw.extract_capacity(fields["Notice Text"])
    # The headline reduction (largest drop), not the 6/30 recovery back to 750,000.
    assert cap["from_capacity"] == 750000.0
    assert cap["to_capacity"] == 650000.0
    assert cap["units"] == "MMBtu/d"
    assert cap["literal"] == "from 750,000 MMBtu/d to 650,000 MMBtu/d"


# --------------------------------------------------------------------------- #
# Notice selection from the category CSVs
# --------------------------------------------------------------------------- #


def test_select_maintenance_notices_keeps_pso():
    selected = tw.select_maintenance_notices({"planned-service-outage": pso_csv()})
    assert len(selected) == 1
    sel = selected[0]
    assert sel["notice_id"] == "118465"
    assert sel["select_reason"] == "planned-service-outage"


# --------------------------------------------------------------------------- #
# Full parse_notice -> NoticeEvent + MaintenanceImpact
# --------------------------------------------------------------------------- #


def test_parse_notice_emits_event_and_impact():
    event, impacts = tw.parse_notice(
        detail_html(), notice_id="118465", pulled_at=PULLED
    )

    # --- NoticeEvent header ---
    assert event.source == "transwestern"
    assert event.notice_id == "118465"
    assert event.notice_type == "planned_outage"
    assert event.notice_type_raw == "Planned Service Outage"
    assert event.severity == "medium"
    assert event.category == "maintenance"
    assert event.status == "initiate"
    assert event.effective_start == "2026-06-16"
    assert event.effective_end == "2026-06-30"
    assert event.posted_at_utc == "2026-06-11 10:38"
    assert event.headline.startswith("East Mainline system")
    assert event.gas_day == "2026-06-16"
    assert event.url.endswith("/ipost/notice/show/118465?asset=TW")
    # Affected location is upstream Station 9, NOT the Topock PG&E delivery.
    assert event.affects_pge is False
    # Orchestrator-owned columns left at their contract defaults.
    assert event.is_current is True
    assert event.has_capacity_impact is False
    assert event.primary_point_id is None
    assert event.prior_notice_id is None

    # --- MaintenanceImpact line ---
    assert len(impacts) == 1
    im = impacts[0]
    assert im.source == "transwestern"
    assert im.maintenance_id == "transwestern:118465"
    assert im.notice_id == "118465"
    assert im.capacity_basis == "remaining"
    # MMBtu/d == Dth/d (no conversion); from=base, to=remaining.
    assert im.base_capacity_dthd == 750000.0
    assert im.capacity_remaining_dthd == 650000.0
    assert im.reduction_dthd == 100000.0
    # pct_of_capacity is strictly remaining/base*100 = 650000/750000*100.
    assert im.pct_of_capacity == 86.7
    assert im.original_value == 650000.0
    assert im.original_units == "MMBtu/d"
    assert im.restriction_type == "Pipeline Maintenance"
    assert im.affected_label == "East Mainline System"
    assert im.work_description.startswith("East Mainline system")
    assert im.date_start == "2026-06-16"
    assert im.date_end == "2026-06-30"
    assert im.is_unplanned is False
    # No numeric loc id on the detail page -> text_label join, no point_id.
    assert im.join_kind == "text_label"
    assert im.point_id is None


def test_impact_row_serializes_all_schema_keys():
    _, impacts = tw.parse_notice(detail_html(), notice_id="118465", pulled_at=PULLED)
    d = impacts[0].to_dict()
    for key in (
        "source", "maintenance_id", "notice_id", "point_id", "segment_or_gate",
        "affected_label", "join_kind", "date_start", "date_end", "capacity_basis",
        "capacity_remaining_dthd", "reduction_dthd", "base_capacity_dthd",
        "pct_of_capacity", "pct_firm_cut", "reduction_planned_dthd",
        "reduction_fm_dthd", "original_value", "original_units", "restriction_type",
        "work_description", "is_unplanned", "pulled_at_utc",
    ):
        assert key in d
