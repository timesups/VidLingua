"""Compatibility command: use vidlingua.pipeline.transcribe for new code."""

from .pipeline.transcribe import *  # noqa: F401,F403
from .pipeline.transcribe import main

if __name__ == "__main__":
    raise SystemExit(main())
