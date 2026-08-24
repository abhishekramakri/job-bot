#!/usr/bin/env python3
"""Union-merge two seen.json files by company key, in place on the second path."""

import json
import sys

run_path, remote_path = sys.argv[1], sys.argv[2]

with open(run_path) as f:
    run_seen = json.load(f)
with open(remote_path) as f:
    remote_seen = json.load(f)

merged = {}
for key in set(run_seen) | set(remote_seen):
    merged[key] = sorted(set(run_seen.get(key, [])) | set(remote_seen.get(key, [])))

with open(remote_path, "w") as f:
    json.dump(merged, f, indent=2)
    f.write("\n")
