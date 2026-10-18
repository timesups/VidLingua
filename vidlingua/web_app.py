"""Compatibility alias for existing imports; the application lives in api.app."""

import sys
from .api import app as _app_module

sys.modules[__name__] = _app_module
