#!/usr/bin/env bash
# Examples of -l/--lines: iterate over the lines of one or more files.
source "$(dirname "$0")/lib.sh"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

printf 'x\ny\nz\n' > "$tmp/a.txt"
printf 'p\nq\n' > "$tmp/b.txt"

out=$(loop -l "$tmp/a.txt" -- echo -n line='{}')
assert_eq "-l iterates the lines of a single file" \
    $'line=x\nline=y\nline=z' "$out"

out=$(loop -l "$tmp/a.txt" "$tmp/b.txt" -- echo -n '{}')
assert_eq "-l concatenates lines across multiple files, in file order" \
    $'x\ny\nz\np\nq' "$out"

summary
