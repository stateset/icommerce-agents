"""The FastAPI host: both commerce-agents roles behind HTTP, over the engine backends."""

from .app import create_app

__all__ = ["create_app"]
