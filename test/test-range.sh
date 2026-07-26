#!/usr/bin/env bash
# Examples of -r/--range: iterate over a numeric range (inclusive, direction inferred).
source "$(dirname "$0")/lib.sh"

out=$(loop -r 1 5 -- echo -n '{}')
assert_eq "-r counts up with default step 1" $'1\n2\n3\n4\n5' "$out"

out=$(loop -r 5 1 -- echo -n '{}')
assert_eq "-r counts down when start > stop" $'5\n4\n3\n2\n1' "$out"

out=$(loop -r 0 10 3 -- echo -n '{}')
assert_eq "-r honors an explicit step" $'0\n3\n6\n9' "$out"

out=$(loop -r 10 0 3 -- echo -n '{}')
assert_eq "-r step is unsigned; direction comes from start vs stop" $'10\n7\n4\n1' "$out"

out=$(loop -r 3 3 -- echo -n '{}')
assert_eq "-r start == stop yields exactly one item" "3" "$out"

out=$(loop -r -5 5 5 -- echo -n '{}')
assert_eq "-r accepts negative numbers" $'-5\n0\n5' "$out"

summary
