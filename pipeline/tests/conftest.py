"""Hypothesis profiles: `default` for every run, `stress` for deliberate deep runs:

uv run pytest pipeline/tests --hypothesis-profile=stress
"""

from hypothesis import settings

settings.register_profile("default", max_examples=150, deadline=None)
settings.register_profile("stress", max_examples=3000, deadline=None)
settings.load_profile("default")
