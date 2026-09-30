"""ChartLens core: domain types, configuration, versioning and point-in-time primitives.

This package must stay free of I/O clients (GCS, Firestore, HTTP). Both the
pipeline and the technical engine depend on it; it depends on neither.
"""

__version__ = "0.1.0"
