"""The built-in assistant: explains concepts and drives the platform's commands."""
from .service import availability, stream_reply

__all__ = ["availability", "stream_reply"]
