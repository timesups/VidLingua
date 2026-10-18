"""Compatibility command: use vidlingua.pipeline.separate_audio for new code."""

from .pipeline.separate_audio import *  # noqa: F401,F403
from .pipeline.separate_audio import main

if __name__ == "__main__":
    raise SystemExit(main())
