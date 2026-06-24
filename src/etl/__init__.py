"""ETL: normalized EBB lineage → star-schema fact partitions + dim CSVs (README §9).

This package turns the per-source normalized records the ``ebb`` clients write to
disk into the Power BI star schema:

- ``load``    — read today's (and the prior day's) normalized lineage from disk.
- ``facts``   — build ``fact_operational`` / ``fact_storage`` dated CSV partitions
                with the pre-computed columns (``dod_change``; storage band /
                net-flow series) folded in.
- ``dims``    — maintain ``dim_pipeline`` / ``dim_cycle`` / ``dim_location`` /
                ``dim_segment``.
- ``publish`` — POST the partition + dim files to Power Automate.

Run on Python 3.11 (Jenkins agent is 3.11.9).
"""
