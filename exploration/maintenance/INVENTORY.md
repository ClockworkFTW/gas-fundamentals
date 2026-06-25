# Maintenance-data inventory (all 7 pipes)

Pulled live 2026-06-24/25. Built by `exploration/extract/<pipe>_maint.py`; structured
output in `exploration/maintenance/<pipe>.json`. Every pipe's extraction was
adversarially verified against raw (capacity numbers + dates re-checked) — all confirmed.
This is reconnaissance/prototyping; NOT wired into src/ETL. Scripts only read; no
`src/ebb` request logic was modified.

## What each pipe exposes, and how good it is for the dashboard

| pipe | access method | rows | capacity impact | units | location join | dates |
|---|---|---|---|---|---|---|
| **pipe_ranger** | `POST /bin/pipeline/foghorn` (redwood+baja × pipelineCap/maxCap/firmCuts) | 25 | **structured** — remaining cap + % of max + % firm cuts | MMcf/d | partial (Topock→`baja_elpaso`/`baja_transw`; Redwood/Kettleman/Hinkley have no point_id) | text, **no year** (inferred) |
| **el_paso** | NoticeDetail.aspx **plain GET**, embedded "Maintenance List" table | **293** | **structured & richest** — base / total reduction / **PLM** (planned) + **FMJ** (force-majeure) / net | **Dth/d** (native) | partial (31/293 rows have numeric loc id; 262 are segment names) | ISO from MM/DD/YY |
| **nova** | TC Customer Express `csv/outages/` + `plantturnaroundactivity/csv/` | 124+25 | **structured** — capability + local base/outage capability; plant impact | 10³m³/d | partial (gate code + area text; no numeric id) | ISO from DD-Mon-YY |
| **foothills** | same NGTL outages CSV, export-gate subset (WGAT/FHZ8→BC, EGAT→SK) | 55 (27 feed PG&E) | **structured** (same as nova) | 10³m³/d | partial (gate code) | ISO |
| **gtn** | notice `Text` prose **+ attached PDF** (`/GTN/Notice/Download/<attId>`, parsed via pdftotext) | 3 notices + 16 PDF rows | **prose + PDF** — available capacity per segment; PDF adds Jun–Sep schedule + qualitative firm-cut risk | MMcf/d | **yes** — `LOC #` ↔ OAC LocationID = `fact_operational.point_id` (all 6 confirmed) | ISO (notice) / literal (PDF) |
| **transwestern** | iPost `notice/show/<id>` **plain GET**, prose | 1 | **prose only** — from→to total station capacity | MMBtu/d (=Dth/d) | no (text label; upstream Station 9, not Topock) | ISO |
| **kern_river** | postback → `NoticeViewer.aspx?DocId=<id>` **GET** (body PDF), zlib-decoded | 17 | **none numeric** — line-pack advisories, prose curtailment only | n/a | no (system-wide) | ISO |

## Per-pipe notes
- **pipe_ranger / foghorn** — forward planned schedule for PG&E's OWN backbone. `Capacity` is
  REMAINING (not the cut); reduction = implied_max − remaining. The dashboard's clearest
  "CGT capacity under maintenance" series. `MaintenanceNotes` = station/pipeline labels.
- **el_paso** — the goldmine. Monthly "Updated <month> Maintenance" notices; each detail page
  is a Word-export HTML table with a structured per-(date-span, scheduling-location) reduction
  series in Dth/d, split into PLM vs FMJ. Notices supersede (take latest non-superseded per month).
  net = base − total_reduction and total = PLM + FMJ hold for all 293 rows.
- **gtn** — prose names one segment + capacity; the attached **Planned Maintenance Schedule PDF**
  is the authoritative multi-segment/multi-month source. `LOC #` in prose AND PDF are real OAC
  LocationIDs (18480 Station 9 CFTP, 954690 Station 6 CFTP, 3500 Flow Past Kingsgate, 18446
  Station 14 CFTP) — directly joinable.
- **nova/foothills** — cleanest CSV; capability is remaining, local base/outage give the cut on
  USJR/compressor rows. Foothills BC (WGAT/FHZ8) is the leg that reaches PG&E.
- **transwestern** — only 1 true maintenance (PSO) in-window; capacity is upstream (Station 9 west-flow),
  not the Topock delivery.
- **kern_river** — postback cracked (NoticeViewer DocId GET, zlib text, no PDF dep), but the notices
  are line-pack/curtailment advisories with NO numeric capacity. Quantified Kern capacity lives in
  its OAC posting (already handled by the client), not in notices. Planned-Service-Outage grid empty.

## Cross-pipe takeaways for the fact_notices / maintenance-timeline design
- **Capacity-impact is structured for 4 of 7** (pipe_ranger, el_paso, nova, foothills) and
  prose-extractable for gtn + transwestern; kern has none.
- **Units diverge 4 ways**: MMcf/d (PR, GTN), Dth/d (EPNG, TW), 10³m³/d (NGTL). Needs source-aware
  normalization — these are NOT pre-normalized like the OAC feeds.
- **"Remaining vs reduction" is inconsistent**: PR/NGTL/GTN report REMAINING capability; EPNG reports
  the REDUCTION (and the net). A common model needs both `capacity_remaining` and `reduction` with a basis flag.
- **Join keys**: GTN (loc id, confirmed) and EPNG (partial loc id) reach `fact_operational.point_id`;
  PR-Topock joins via dim_location; NGTL/foothills join by gate code; TW/Kern only by text label.
