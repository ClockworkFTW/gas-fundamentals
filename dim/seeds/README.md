# dim/seeds — schematic topology seeds

These CSVs are folded into `dim/dim_location.csv` and `dim/dim_segment.csv` by
`src/etl/dims.py` (`build_dims` globs `dim/seeds/*_nodes.csv` / `*_segments.csv`).
They define the nodes and edges of the Power BI **Deneb hero schematic** and the
per-pipeline drill (README §7): western supply → PG&E Citygate → demand.

Rebuild after editing: `.\.venv\Scripts\python.exe -m etl.dims` (run from repo root
with `src` on the path), or it runs as part of `python -m src`.

## Scope: the major interconnect spine

This is the **condensed** topology — only the major receipt/delivery interconnects
(borders + handoffs) and the PG&E core (Citygate, demand, storage), wired by the
segments between them. The granular per-basin supply receipts (El Paso's 75 points,
etc.) are intentionally **not** seeded; per-point detail lives in the Power BI
"Operational Capacity table" (README §7). 17 nodes / 16 edges.

To restore per-pipeline basin detail later (for the drill view) without re-cluttering
the hero map, add the basin nodes back with an extra `tier` column (`hero`/`detail`)
and filter the hero schematic to `tier = 'hero'`.

## Files

One pair per pipeline that owns interconnect nodes: `pipe_ranger`, `gtn`,
`transwestern`, `kern_river`, `foothills`.

- `<pipeline>_nodes.csv` → columns `pipeline,point_id,x,y,type,label,zone`
- `<pipeline>_segments.csv` → columns `pipeline,segment_id,from_node,to_node,path_kind`

**Not seeded (their flow still shows):** `el_paso` has no delivery record of its own,
so it's carried on Pipe Ranger's `baja_elpaso` (`Topock - PG&E (El Paso)`) border;
`nova`/NGTL collapses to the `foothills` `AB/BC` export node; `ruby` is inactive (shown
on Pipe Ranger's `onyx_ruby`); `eia` is macro storage with no schematic node.

## The one rule that matters: `point_id` must join

`dim_location.point_id` is joined to `fact_operational.point_id`, which is
`FlowRecord.point_id` **verbatim** (`src/etl/facts.py:_operational_rows`). Every
`point_id` here is copied byte-for-byte from real EBB output (verified against
`tests/golden/`). **Do not normalize, pad, re-case, or strip** — especially the
literal-text id `AB/BC` (it has a slash, not a numeric Loc code). Any edit that
changes a `point_id` silently produces an empty join (a blank node). The test
`tests/test_etl_dims.py::test_committed_topology_seeds_are_coherent` guards this.

## `type` vocabulary

| type | joins to | meaning |
|---|---|---|
| `supply` | `fact_operational` (receipt) | producing-basin / upstream receipt |
| `handoff` | `fact_operational` (delivery) | a pipeline's delivery into the next system |
| `border` | `fact_operational` (receipt) | PG&E's receipt at the CA border (Malin/Topock/Daggett) |
| `demand` | `fact_operational` (supply_demand) | Core / System demand |
| `balance` | `fact_operational` (supply_demand) | System Supply (balance counterpart) |
| `storage` | **`fact_storage`** (see note) | independent CA storage facility |
| `hub` | **nothing** (synthetic) | `cgt_citygate` — implicit Citygate node |

## Layout & join notes (decisions baked into these seeds)

- **Coordinates** are a normalized `0–100` grid: `x` = supply (west, left) → demand
  (right); `y` = north (top, Canada/Malin) → south (bottom, Topock/SW). Purely
  cosmetic — retune freely; the tests assert structure, not positions.
- **Citygate is synthetic.** No `point_id` for Citygate exists in any EBB; node
  `cgt_citygate` is a layout hub that intentionally joins to nothing. Derive its
  throughput in DAX (Σ border receipts − demand).
- **El Paso has no CA-delivery record** (receipts-only EBB), so it isn't seeded
  directly; Pipe Ranger's `baja_elpaso` border (`Topock - PG&E (El Paso)`) is El
  Paso's delivered-volume node.
- **Transwestern `56698` appears twice** (a delivery row with flow + a null receipt
  row, same id). Filter `flow_direction = 'delivery'` in the visual to avoid a blank
  duplicate.
- **`AB/BC` also exists under `source='nova'`** (foothills is a re-tag of the same
  NGTL feed). Only `foothills` `AB/BC` is seeded here, but still key the model on
  **(pipeline, point_id)** so a future `nova` seed can't double-render the Canadian
  export. It carries the AB/BC→Kingsgate leg into GTN `3498`.
- **Storage nodes join to `fact_storage`, not `fact_operational`.** Their ids
  (`WG_Net_Inj`, `Lodi_Net_Inj`, `CVGS_Inj_Phys`, `GRS_LLC_Inj`) are real Pipe Ranger
  storage points, but `fact_storage` currently carries only an aggregated **PG&E
  System** row (no per-facility rows). They render as labeled schematic nodes today;
  lighting them up with per-facility flow needs `fact_storage` to emit per-facility
  partitions (a separate ETL change).
- **Utilization gauges** need a capacity denominator. The flow ids here come from the
  scheduled-volume datasets; Pipe Ranger exposes capacity under *different* ids
  (`Phys_PGT_NW`, `El_Paso_Phys`, `TW_Phys_Cap`, `KRGT_Daggett_Phys`, …). To color an
  edge by % of capacity, map each border node's flow id to its capacity id in DAX.

## Coverage / what's intentionally omitted

The condensed spine is **major interconnects only**. Omitted: all per-basin supply
receipts (El Paso's 75, Transwestern's San Juan/Permian meters, Kern's Rockies/Opal
receipts, NGTL's field receipts — per-point detail lives in the Power BI "Operational
Capacity table", README §7); Foothills' eastern Empress/McNeill borders (off the PG&E
path); Kern's secondary Fremont Peak delivery (`345606`); the `Supply_Sys` balance
metric and `MeanTemp` (°F, not a flow); path-total rollups (`redwood_total`,
`baja_total`) that would double-count their components.
