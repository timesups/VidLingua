"""Compatibility command: use vidlingua.pipeline.process_video for new code."""

from .pipeline.process_video import *  # noqa: F401,F403
from .pipeline.process_video import main

if __name__ == "__main__":
    raise SystemExit(main())
