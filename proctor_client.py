"""PyInstaller entry point for the proctoring client.

This thin wrapper exists so PyInstaller has a single script to analyze. It
imports the real client package and runs it. Build with:

    pyinstaller proctor-client.spec
"""

import sys

from client.main import main

if __name__ == "__main__":
    sys.exit(main())
