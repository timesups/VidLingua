"""VidLingua's video processing and local web application."""

from importlib import import_module

_LEGACY_MODULES = {
    "azure_dubbing": "providers.azure_speech",
    "deepseek_translation": "providers.deepseek",
    "prepare_translation": "pipeline.preparation",
    "subtitle_burn": "rendering.subtitles",
    "watermark": "rendering.watermark",
}


def __getattr__(name: str):
    if name in _LEGACY_MODULES:
        return import_module(f".{_LEGACY_MODULES[name]}", __name__)
    raise AttributeError(name)
