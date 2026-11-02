"""One entry point for the desktop application and its owned background workers."""
import argparse
from contextlib import contextmanager
import importlib
import os
from pathlib import Path
import sys

from .runtime import WORKERS, self_check


@contextmanager
def usable_streams():
    """Windowed builds have no console streams; libraries may still print/flush."""
    opened = []
    try:
        for name, mode in (("stdin", "r"), ("stdout", "w"), ("stderr", "w")):
            if getattr(sys, name) is None:
                stream = open(os.devnull, mode, encoding="utf-8")
                setattr(sys, name, stream)
                opened.append((name, stream))
        yield
    finally:
        for name, stream in opened:
            setattr(sys, name, None)
            stream.close()


def main(argv=None):
    with usable_streams():
        return dispatch(argv)


def dispatch(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["--worker"]:
        if len(arguments) < 2 or arguments[1] not in WORKERS:
            raise SystemExit("Unknown application worker")
        kind = arguments[1]
        original = sys.argv
        try:
            sys.argv = [original[0], *arguments[2:]]
            module = importlib.import_module(f"aion2_redbag_map.{WORKERS[kind]}")
            return module.main()
        finally:
            sys.argv = original
    parser = argparse.ArgumentParser(description="AION2 Hidden Cube Radar")
    parser.add_argument("--port", type=int, default=18726)
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--self-check", type=Path, metavar="PATH")
    parser.add_argument("--start-receiving", action="store_true", help="Explicitly start receiving after opening the manager")
    parser.add_argument("--show-overlay", action="store_true", help="Show the managed desktop map")
    parser.add_argument("--minimized", action="store_true", help="Open the manager minimized")
    args = parser.parse_args(arguments)
    if not 1024 <= args.port <= 65535:
        parser.error("invalid local port")
    if args.self_check:
        return 0 if self_check(args.self_check)["ok"] else 1
    from .desktop import run
    return run(args.port, args.data_dir, start_receiving=args.start_receiving,
               show_overlay=args.show_overlay, minimized=args.minimized)


if __name__ == "__main__":
    raise SystemExit(main())
