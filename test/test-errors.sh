#!/usr/bin/env bash
# Examples of loop's error handling and exit codes.
source "$(dirname "$0")/lib.sh"

exit=0
loop -L a b -- true >/dev/null || exit=$?
assert_status "loop exits 0 when every item's body succeeds" 0 "$exit"

exit=0
loop -L a b -- false >/dev/null || exit=$?
assert_status "loop exits nonzero if any item's body fails" 1 "$exit"

exit=0
loop -L a -- true >/dev/null 2>&1 || exit=$?
assert_status "sanity: single successful item still exits 0" 0 "$exit"

exit=0
loop -L a b >/dev/null 2>&1 || exit=$?
assert_status "missing '--' <body> is a usage error" 2 "$exit"

exit=0
loop a b -- echo '{}' >/dev/null 2>&1 || exit=$?
assert_status "missing an -f/-l/-r/-L iterator flag is a usage error" 2 "$exit"

summary
