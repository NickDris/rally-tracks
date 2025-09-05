#!/usr/bin/env python3
import yaml
import sys

if len(sys.argv) != 2:
    print("Usage: get_tracks.py <filters.yml>", file=sys.stderr)
    sys.exit(1)

filters_file = sys.argv[1]
with open(filters_file) as f:
    filters = yaml.safe_load(f)
tracks = list(filters.keys())
print(",".join(tracks))
