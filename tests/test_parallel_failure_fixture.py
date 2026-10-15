#!/usr/bin/env python3
"""Intentional child failure used by the scheduler regression."""

import sys


if __name__ == "__main__":
    print("intentional scheduler child failure")
    sys.exit(7)
