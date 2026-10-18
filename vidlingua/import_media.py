"""Compatibility command: use vidlingua.pipeline.import_media for new code."""

from .pipeline.import_media import *  # noqa: F401,F403
from .pipeline.import_media import main

if __name__ == "__main__":
    raise SystemExit(main())
