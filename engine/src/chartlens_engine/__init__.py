"""ChartLens technical-analysis engine.

Pure computation: bar frames in, typed results out. No file, network, database
or clock access — ``as_of`` is always passed in, never read from ``now()``.
The engine does not know which exchange its data came from.

Subpackages (indicators, swings, structure, patterns, ...) are added phase by
phase; each implements :class:`chartlens_engine.interfaces.Analyzer`.
"""

__version__ = "0.1.0"
