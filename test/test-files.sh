#!/usr/bin/env bash
# Examples of -f/--files: iterate over glob-matched filenames, including ** recursion.
source "$(dirname "$0")/lib.sh"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/sub"
: > "$tmp/a.txt"
: > "$tmp/b.txt"
: > "$tmp/sub/c.txt"

pushd "$tmp" >/dev/null

out=$(loop -f '*.txt' -- echo -n '{}')
assert_eq "-f expands a flat glob" $'a.txt\nb.txt' "$out"

out=$(loop -f '**/*.txt' -- echo -n '{}')
assert_eq "-f expands ** recursively (matches top-level and subdirs)" \
    $'a.txt\nb.txt\nsub/c.txt' "$out"

out=$(loop -f '*.txt' 'sub/*.txt' -- echo -n '{}')
assert_eq "-f accepts multiple patterns, concatenated in order given" \
    $'a.txt\nb.txt\nsub/c.txt' "$out"

popd >/dev/null
summary
