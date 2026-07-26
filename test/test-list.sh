#!/usr/bin/env bash
# Examples of -L/--list: iterate over literal strings given on the command line.
source "$(dirname "$0")/lib.sh"

out=$(loop -L red green blue -- echo -n color '{}')
assert_eq "-L iterates literal args in order" \
    $'color red\ncolor green\ncolor blue' "$out"

out=$(loop -L only-one -- echo -n '{}')
assert_eq "-L works with a single item" "only-one" "$out"

# A scriptlet body: multiple positional args are space-joined, and may pipe.
out=$(loop -L one two three -d '' -- "echo {} | tr a-z A-Z")
assert_eq "body may be a pipeline built from multiple positional args" \
    $'ONE\nTWO\nTHREE' "$out"

summary
