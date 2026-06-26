# studio_console/__init__.py
"""Studio Console — operator management CLI."""

from pathlib import Path

_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
__version__: str = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "dev"


def main() -> None:
    from .cli import main as _main

    _main()


__all__ = ["main", "__version__"]

