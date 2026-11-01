"""PyInstaller entry; the source and frozen applications use the same dispatch."""
from aion2_redbag_map.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
