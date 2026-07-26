#!/usr/bin/env bash
# Examples of -j/--jobs: run the body concurrently via GNU parallel.
# --keep-order means output order matches iteration order, same as serial.
source "$(dirname "$0")/lib.sh"

if ! command -v parallel >/dev/null; then
    echo "skip - GNU parallel not installed, skipping -j tests"
    exit 0
fi

out=$(loop -j -L a b c -- echo -n '{}')
assert_eq "-j (bare, auto worker count) preserves iteration order" \
    $'a\nb\nc' "$out"

out=$(loop -j 2 -r 1 6 -- echo -n '{}')
assert_eq "-j N runs with N workers, still ordered" \
    $'1\n2\n3\n4\n5\n6' "$out"

exit=0
loop -j -L a b -- false || exit=$?
assert_status "-j propagates a job failure to loop's own exit code" 1 "$exit"

summary
