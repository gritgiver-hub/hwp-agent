#!/usr/bin/env python3
"""Entrypoint for the local HWP agent CLI (Windows + Hancom)."""
import sys

from hwp_agent.cli import main

if __name__ == "__main__":
    sys.exit(main())
