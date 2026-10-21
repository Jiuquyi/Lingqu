#!/usr/bin/env bash
set -euo pipefail
# update_locks.sh - compile pip-tools lock (*.txt) files from *.in specs.
# Usage examples:
#   ./scripts/update_locks.sh            # update all (base dev docker)
#   ./scripts/update_locks.sh dev        # only dev
#   U=1 ./scripts/update_locks.sh base   # upgrade all transitive under base
#   UPKG=Flask,SQLAlchemy ./scripts/update_locks.sh base dev
#   HASHES=1 ./scripts/update_locks.sh   # include hashes

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REQ_DIR="$ROOT_DIR/requirements"
cd "$ROOT_DIR"

declare -a ALL_BASENAMES=(base dev docker)

if [[ $# -gt 0 ]]; then
  TARGETS=("$@")
else
  TARGETS=("${ALL_BASENAMES[@]}")
fi

EXTRA_ARGS=()
[[ "${HASHES:-}" == 1 ]] && EXTRA_ARGS+=(--generate-hashes)
[[ "${U:-}" == 1 ]] && EXTRA_ARGS+=(--upgrade)
if [[ -n "${UPKG:-}" ]]; then
  IFS=',' read -r -a _pkgs <<< "$UPKG"
  for p in "${_pkgs[@]}"; do
    EXTRA_ARGS+=(--upgrade-package "$p")
  done
fi

echo "[lock] Compiling: ${TARGETS[*]}" >&2
python -m pip install -q -U pip pip-tools >/dev/null 2>&1

FAILED=0
for name in "${TARGETS[@]}"; do
  in_file="$REQ_DIR/${name}.in"
  out_file="$REQ_DIR/${name}.txt"
  if [[ ! -f "$in_file" ]]; then
    echo "[skip] missing $in_file" >&2
    continue
  fi
  echo "[compile] $in_file -> $(basename "$out_file")" >&2
  if ! pip-compile "$in_file" -o "$out_file" "${EXTRA_ARGS[@]}"; then
    echo "[error] failed: $in_file" >&2
    FAILED=1
  fi
done

if [[ $FAILED -eq 0 ]]; then
  echo "[done] lock files updated." >&2
else
  echo "[partial] some lock updates failed." >&2
  exit 1
fi
