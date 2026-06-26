#!/usr/bin/env python3
# studio-console.py
"""Studio Console — operator management CLI entry point.

Standalone distribution: https://github.com/selfhosthub/studio-console
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from studio_console.cli import main

if __name__ == "__main__":
    main()
