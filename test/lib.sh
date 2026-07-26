#!/usr/bin/env bash
# Shared helpers for test/test-*.sh.  Source this, don't execute it directly.
set -u

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Run loop via uv from whatever directory the caller is in (so relative
# globs/paths in a test's tmpdir resolve correctly), always against this repo.
loop() { uv run --project "$REPO_ROOT" loop "$@"; }

_pass=0
_fail=0

# assert_eq DESC EXPECTED ACTUAL
assert_eq() {
    local desc=$1 expected=$2 actual=$3
    if [[ "$expected" == "$actual" ]]; then
        echo "ok - $desc"
        _pass=$((_pass + 1))
    else
        echo "not ok - $desc"
        printf '  expected: %q\n  actual:   %q\n' "$expected" "$actual"
        _fail=$((_fail + 1))
    fi
}

# assert_status DESC EXPECTED_CODE ACTUAL_CODE
assert_status() { assert_eq "$1 (exit code)" "$2" "$3"; }

# assert_file_eq DESC EXPECTED_FILE ACTUAL_FILE
# Byte-exact comparison; use this instead of assert_eq whenever output may
# contain NUL bytes (bash variables can't hold them, so $(...) would lose them).
assert_file_eq() {
    local desc=$1 expected=$2 actual=$3
    if cmp -s "$expected" "$actual"; then
        echo "ok - $desc"
        _pass=$((_pass + 1))
    else
        echo "not ok - $desc"
        echo "  expected: $(od -An -c "$expected" | tr -s ' ')"
        echo "  actual:   $(od -An -c "$actual" | tr -s ' ')"
        _fail=$((_fail + 1))
    fi
}

# Print pass/fail totals and exit with the summarizing script's true status.
summary() {
    echo
    echo "$_pass passed, $_fail failed  ($(basename "$0"))"
    [[ "$_fail" -eq 0 ]]
}
