"""Offline tests for the Foothills maintenance source (etl.maintenance_sources.foothills).

Foothills has no own feed: its rows are the export-gate subset of the NGTL/NOVA
outages table, tagged by leg. The fixture is the saved NOVA outages CSV
(tests/fixtures/maint/foothills_outages.csv, copied from
exploration/notices/foothills/raw/outages.csv). We assert that ONLY the export
gates (WGAT/FHZ8 -> Foothills BC, EGAT -> Foothills SK) survive and that the leg
tag is prefixed onto ``affected_label``, against values confirmed in the raw CSV.
"""
import pathlib

import pytest

from ebb.schema import MaintenanceImpact
from etl.maintenance_sources import foothills

FIX = pathlib.Path(__file__).parent / "fixtures" / "maint"
PULLED = "2026-06-25T05:26:30Z"
# gas_day on the first day of the feed so the -7/+45 day window keeps the early
# export-gate outages (the NOVA parser applies that window).
GAS_DAY = "2026-06-22"


def outages_text() -> str:
    return (FIX / "foothills_outages.csv").read_text(encoding="utf-8")


@pytest.fixture
def impacts() -> list[MaintenanceImpact]:
    return foothills.parse_outages(outages_text(), GAS_DAY, PULLED)


# --------------------------------------------------------------------------- #
# Only export-gate rows survive
# --------------------------------------------------------------------------- #


def test_only_export_gates_survive(impacts):
    assert impacts, "expected export-gate outages within the window"
    gates = {im.segment_or_gate for im in impacts}
    assert gates == {"WGAT", "FHZ8", "EGAT"}
    # internal NGTL areas in the same CSV (receipt corridors, laterals, other
    # delivery areas) must NOT leak through.
    assert not (gates & {"USJR", "LCLR", "NEDA", "OSDA", "LCLD"})


def test_all_rows_tagged_foothills(impacts):
    assert all(im.source == "foothills" for im in impacts)
    # every kept row is tagged with its leg, prefixed onto the label.
    assert all(im.affected_label.startswith(("[Foothills BC] ", "[Foothills SK] ")) for im in impacts)


def test_leg_mapping(impacts):
    by_gate_leg = {
        im.segment_or_gate: im.affected_label.split("]")[0] + "]" for im in impacts
    }
    assert by_gate_leg["WGAT"] == "[Foothills BC]"
    assert by_gate_leg["FHZ8"] == "[Foothills BC]"
    assert by_gate_leg["EGAT"] == "[Foothills SK]"


def test_counts_by_gate(impacts):
    # Confirmed against the raw CSV for the 2026-06-22 window (-7/+45 days):
    # 10 WGAT + 5 FHZ8 (Foothills BC) + 13 EGAT (Foothills SK).
    by_gate = {}
    for im in impacts:
        by_gate[im.segment_or_gate] = by_gate.get(im.segment_or_gate, 0) + 1
    assert by_gate == {"WGAT": 10, "FHZ8": 5, "EGAT": 13}
    bc = [im for im in impacts if im.affected_label.startswith("[Foothills BC]")]
    sk = [im for im in impacts if im.affected_label.startswith("[Foothills SK]")]
    assert len(bc) == 15  # WGAT + FHZ8 -> reaches PG&E
    assert len(sk) == 13  # EGAT -> eastbound, no PG&E


# --------------------------------------------------------------------------- #
# Specific row values (capacity numbers + dates verified against the raw CSV)
# --------------------------------------------------------------------------- #


def test_first_wgat_row_values(impacts):
    # outages.csv line 49: WGAT outage 20216834 / UID 202168345, 22-Jun..26-Jun-26,
    # Capability 87000 10^3m^3/d.
    w = next(im for im in impacts if im.maintenance_id == "foothills:202168345")
    assert w.segment_or_gate == "WGAT"
    assert w.affected_label == "[Foothills BC] Alberta/BC and Alberta/Montana Borders"
    assert w.date_start == "2026-06-22"
    assert w.date_end == "2026-06-26"
    # capacity_basis is inherited remaining; original is preserved, normalized to Dth/d.
    assert w.capacity_basis == "remaining"
    assert w.original_value == 87000.0
    assert w.original_units == "10^3m^3/d"
    assert w.capacity_remaining_dthd == 3072376.0  # 87000 * 35314.666721 * 1000 / 1e6
    # no true unconstrained base in the export-gate feed -> base/pct stay null.
    assert w.base_capacity_dthd is None
    assert w.pct_of_capacity is None


def test_fhz8_potential_impact_row(impacts):
    # outages.csv line 70: FHZ8 outage 20168934 / UID 201689341, 13-Jul..15-Jul-26,
    # Capability 83000, "Potential Impact to FT", Alberta/BC Border.
    f = next(im for im in impacts if im.maintenance_id == "foothills:201689341")
    assert f.segment_or_gate == "FHZ8"
    assert f.affected_label == "[Foothills BC] Alberta/BC Border"
    assert f.date_start == "2026-07-13"
    assert f.date_end == "2026-07-15"
    assert f.original_value == 83000.0
    assert f.restriction_type == "Potential Impact to FT"


def test_egat_is_foothills_sk(impacts):
    # outages.csv line 76: EGAT outage 20216834 / UID 202168344, Empress/McNeill.
    e = next(im for im in impacts if im.maintenance_id == "foothills:202168344")
    assert e.segment_or_gate == "EGAT"
    assert e.affected_label == "[Foothills SK] Empress/McNeill Borders"
    assert e.original_value == 142000.0


# --------------------------------------------------------------------------- #
# Pure filter behavior on a tiny hand-built CSV
# --------------------------------------------------------------------------- #


def test_internal_only_outage_dropped():
    # A row whose only gate is an internal area (OSDA) is dropped entirely.
    csv = (
        "Outage Id,Table,Start,End,Capability,Typical Flow,Type of Restriction,"
        "Area for Stated Capability,Other Restricted Segments,Description,UID,"
        "Local Base Capability,Local Outage Capability\n"
        "9,OSDA,20-Jun-26,25-Jun-26,85000,,FT-D,Segments 10 11,,Internal only,901,,\n"
    )
    assert foothills.parse_outages(csv, GAS_DAY, PULLED) == []


def test_wgat_only_row_tagged_bc():
    csv = (
        "Outage Id,Table,Start,End,Capability,Typical Flow,Type of Restriction,"
        "Area for Stated Capability,Other Restricted Segments,Description,UID,"
        "Local Base Capability,Local Outage Capability\n"
        "5,WGAT,20-Jun-26,25-Jun-26,87000,,No impact to FT-D anticipated,"
        "Alberta/BC Border,,West gate work,55,,\n"
    )
    rows = foothills.parse_outages(csv, GAS_DAY, PULLED)
    assert len(rows) == 1
    assert rows[0].affected_label == "[Foothills BC] Alberta/BC Border"
