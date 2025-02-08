#!/usr/bin/env python3

from pathlib import Path

import tomllib

##################
# --- CONFIG --- #
##################
with Path("config.toml").open(mode="rb") as fp:
    CONFIG = tomllib.load(fp)
