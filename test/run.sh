#!/usr/bin/env bash
# Run every test/test-*.sh and print an overall summary.
#
#   test/run.sh
set -u
dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

overall=0
for t in "$dir"/test-*.sh; do
    echo "== $(basename "$t") =="
    bash "$t" || overall=1
    echo
done

exit "$overall"
