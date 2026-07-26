#!/usr/bin/env bash
# Examples of -s/--shell: choose the interpreter that runs <body>.
# Redirection (-o/-e) and the delimiter (-d) are applied outside the chosen
# shell, so they work the same regardless of which interpreter runs <body>.
source "$(dirname "$0")/lib.sh"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

# -d '' turns off the delimiter, so each body's own newline is the only one
# in the output (see test-delim.sh for how -d normally adds its own).
out=$(loop -L a b -s bash -d '' -- 'echo bash:{}')
assert_eq "-s bash runs <body> as a bash -c script" $'bash:a\nbash:b' "$out"

if command -v python3 >/dev/null; then
    out=$(loop -L a b -s python3 -d '' -- "print('py:{}')")
    assert_eq "-s python3 runs <body> as Python code, not shell syntax" \
        $'py:a\npy:b' "$out"

    loop -L a -s python3 -o "$tmp/py-out.txt" -- "print('hi {}')"
    assert_eq "-o still redirects output when --shell is non-POSIX (python)" \
        "hi a" "$(cat "$tmp/py-out.txt")"
fi

summary
