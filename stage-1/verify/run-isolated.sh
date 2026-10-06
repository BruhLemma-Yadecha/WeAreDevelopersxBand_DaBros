#!/bin/sh
# Build a clean temporary source copy and assay it with OS-level net isolation.
set -eu

stage_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
scratch=$(mktemp -d)
cleanup() { rm -rf "$scratch"; }
trap cleanup EXIT HUP INT TERM

mkdir "$scratch/source" "$scratch/data"
cp "$stage_dir/app.py" "$scratch/source/app.py"
cp "$stage_dir/verify/assay_stage1.py" "$scratch/assay_stage1.py"

# A fresh user+network namespace has no external interfaces.  Loopback is
# explicitly enabled solely so the verifier can talk to its own server.
unshare --user --map-root-user --net -- sh -eu -c '
  ip link set lo up
  cd "$1"
  python3 -m py_compile app.py
  python3 "$2" "$1" "$3"
' sh "$scratch/source" "$scratch/assay_stage1.py" "$scratch/data"
