"""Canonical rover entry point; the mapping module remains compatible."""

from .mapping.server import app, create_app, main

__all__ = ["app", "create_app", "main"]

if __name__ == "__main__":
    main()
