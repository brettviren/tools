#!/usr/bin/env bash
# Examples of -o/--output and -e/--error: redirect each item's stdout/stderr
# to its own file, with %d (0-based index) and {} (item) substituted.
source "$(dirname "$0")/lib.sh"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
pushd "$tmp" >/dev/null

loop -L x y -o 'out-%d-{}.txt' -e 'err-%d-{}.txt' \
    -- 'echo out {}; echo err {} >&2'

assert_eq "-o writes item 0's stdout to its own file" \
    "out x" "$(cat out-0-x.txt)"
assert_eq "-o writes item 1's stdout to its own file" \
    "out y" "$(cat out-1-y.txt)"
assert_eq "-e writes item 0's stderr to its own file" \
    "err x" "$(cat err-0-x.txt)"
assert_eq "-e writes item 1's stderr to its own file" \
    "err y" "$(cat err-1-y.txt)"

# With -o given, -d/--delim no longer applies to stdout: nothing extra is
# appended to the loop's own stdout stream (there's nothing left to join).
out=$(loop -L p q -d , -o 'quiet-%d.txt' -- echo hi)
assert_eq "-o silences loop's own stdout (nothing to delimit)" "" "$out"

popd >/dev/null
summary
