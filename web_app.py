"""Keep the existing ``uvicorn web_app:app`` entry point working."""

import sys

from vidlingua import web_app as _implementation

sys.modules[__name__] = _implementation
