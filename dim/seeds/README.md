# dim/seeds — schematic topology seeds

These CSVs are folded into `dim/dim_location.csv` and `dim/dim_segment.csv` by
`src/etl/dims.py` (`build_dims` globs `dim/seeds/*_nodes.csv` / `*_segments.csv`).
They define the nodes and edges of the Power BI **Deneb hero schematic** and the
per-pipeline drill (README §7): western supply → PG&E Citygate → demand.

Rebuild after editing: `.\.venv\Scripts\python.exe -m etl.dims` (run from repo root
with `src` on the path), or it runs as part of `python -m src`.

## Files

One pair per **flow pipeline** (`eia` is macro storage with no nodes; `ruby` is
inactive — its flow shows on Pipe Ranger's `onyx_ruby` border node):
`pipe_ranger`, `gtn`, `el_paso`, `transwestern`, `kern_river`, `nova`, `foothills`.

- `<pipeline>_nodes.csv` → columns `pipeline,point_id,x,y,type,label,zone`
- `<pipeline>_segments.csv` → columns `pipeline,segment_id,from_node,to_node,path_kind`

## The one rule that matters: `point_id` must join

`dim_location.point_id` is joined to `fact_operational.point_id`, which is
`FlowRecord.point_id` **verbatim** (`src/etl/facts.py:_operational_rows`). Every
`point_id` here is copied byte-for-byte from real EBB output (verified against
`tests/golden/`). **Do not normalize, pad, re-case, or strip** — especially the
literal-text ids (`AB/BC` has a slash; `NGTL-Field Receipts` a hyphen;
`Mcneil Border Flow` is spelled with one `l`). Any edit that changes a `point_id`
silently produces an empty join (a blank node). The test
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
- **El Paso has no CA-delivery record** (receipts-only EBB), so its receipts wire
  straight to PG&E's `baja_elpaso` border node; Pipe Ranger owns the Topock handoff.
- **Transwestern `56698` appears twice** (a delivery row with flow + a null receipt
  row, same id). Filter `flow_direction = 'delivery'` in the visual to avoid a blank
  duplicate.
- **`nova` and `foothills` share `point_id` `AB/BC`** (foothills is a re-tag of NGTL
  feeds). Key the model on **(pipeline, point_id)**, not `point_id` alone. The hero
  schematic should render the Canadian export **once** — use `foothills` `AB/BC` (it
  carries the AB/BC→Kingsgate leg into GTN `3498`); `nova`'s `AB/BC` is the terminus
  of NOVA's own drill subgraph.
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

Each pipeline carries its CA handoff + ~6 representative supply nodes — **not** every
operational point (El Paso alone emits 75). The per-point detail lives in the Power BI
"Operational Capacity table" (README §7). Omitted: Foothills' eastern Empress/McNeill
borders are present as context nodes but off the PG&E path; `MeanTemp` (a °F driver,
not a flow node); path-total rollups (`redwood_total`, `baja_total`) that would
double-count their components.
