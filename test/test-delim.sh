#!/usr/bin/env bash
# Examples of -d/--delim: the separator placed after each item's stdout
# (only meaningful when -o/--output is NOT also given).
source "$(dirname "$0")/lib.sh"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

out=$(loop -L a b c -- echo -n '{}')
assert_eq "default delim is a newline" $'a\nb\nc' "$out"

out=$(loop -L a b c -d , -- echo -n '{}')
assert_eq "a literal delimiter string is used as-is" "a,b,c," "$out"

# NUL and other control characters can't survive a bash variable, so compare
# raw bytes on disk instead of via $(...).
loop -L a b c -d NUL -- printf '%s' '{}' > "$tmp/actual-nul"
printf 'a\0b\0c\0' > "$tmp/expected-nul"
assert_file_eq "-d NUL emits a real NUL byte after each item" \
    "$tmp/expected-nul" "$tmp/actual-nul"

loop -L a b -d TAB -- printf '%s' '{}' > "$tmp/actual-tab"
printf 'a\tb\t' > "$tmp/expected-tab"
assert_file_eq "-d TAB emits a real tab byte after each item" \
    "$tmp/expected-tab" "$tmp/actual-tab"

# Gotcha: the delimiter is appended after whatever the body itself printed,
# so a plain `echo` (which prints its own trailing newline) plus the default
# newline delimiter gives a blank line between items. Use `echo -n`, or -d '',
# to avoid the doubling; see the other test-*.sh files for that pattern.
out=$(loop -L a b -- echo '{}')
assert_eq "plain echo + default delim stacks two newlines between items" \
    $'a\n\nb' "$out"

summary
