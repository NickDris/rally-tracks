import yaml
import sys
import os

if len(sys.argv) != 2:
    print("Usage: detect-changed-tracks.py <filters.yml>", file=sys.stderr)
    sys.exit(1)

filters_file = sys.argv[1]
with open(filters_file) as f:
    filters = yaml.safe_load(f)

changed_tracks = []
for track in filters.keys():
    # GitHub Actions exposes outputs as environment variables in the form 'track'
    if os.environ.get(track) == "true":
        changed_tracks.append(track)

print(",".join(changed_tracks))
