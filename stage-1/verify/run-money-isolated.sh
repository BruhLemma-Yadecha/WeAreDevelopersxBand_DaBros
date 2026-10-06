#!/bin/sh
# Fresh source copy; the service and all attacks run without an external NIC.
set -eu

stage_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
scratch=$(mktemp -d)
cleanup() { rm -rf "$scratch"; }
trap cleanup EXIT HUP INT TERM

mkdir "$scratch/source" "$scratch/data"
cp "$stage_dir/app.py" "$scratch/source/app.py"
cp "$stage_dir/verify/assay_stage1_money.py" "$scratch/assay_stage1_money.py"
cp "$stage_dir/verify/sitecustomize_commit_fault.py" "$scratch/source/sitecustomize.py"

unshare --user --map-root-user --net -- sh -eu -c '
  ip link set lo up
  cd "$1"
  python3 -m py_compile app.py
  FAIL_COMMIT_MARKER="$4" python3 "$2" "$1" "$3"
' sh "$scratch/source" "$scratch/assay_stage1_money.py" "$scratch/data" "$scratch/fail-next-commit"
