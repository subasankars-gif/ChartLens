"""ChartLens job layer (ADR-0001 as amended by ADR-0024, ADR-0025).

Orchestration that needs both the pipeline and the engine: the tracked production run
and its ANALYSIS stage. It imports ``core``, ``engine`` and ``pipeline``; none of them,
and not the API, imports it.
"""
