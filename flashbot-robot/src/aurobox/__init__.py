"""Aurobox Flashbot Python package."""

__all__ = [
    "FlashbotController",
    "PuduApiClient",
    "load_config",
    "create_app",
    "db",
]

__version__ = "0.4.0"


def __getattr__(name):
    if name == "FlashbotController":
        from .robot import FlashbotController

        return FlashbotController
    if name == "PuduApiClient":
        from .pudu_client import PuduApiClient

        return PuduApiClient
    if name == "load_config":
        from .config import load_config

        return load_config
    if name == "create_app":
        from .app import create_app

        return create_app
    if name == "db":
        from .models import db

        return db
    raise AttributeError(f"module 'aurobox' has no attribute {name!r}")
