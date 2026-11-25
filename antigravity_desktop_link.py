"""Entry point launched by Antigravity App's Sidecar manager."""
import argparse
from pathlib import Path

from bridge.antigravity import run_sidecar


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", required=True, type=Path)
    run_sidecar(parser.parse_args().state)
