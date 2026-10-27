#!/bin/bash
# lib_assert.sh - tiny assertion helpers shared by the bash test files.
# Source it; call assert_*; end with `finish` (exit code = 0 iff all passed).

TESTS_RUN=0
TESTS_FAIL=0

_pass() { TESTS_RUN=$((TESTS_RUN + 1)); printf '  ok   %s\n' "$1"; }
_fail() {
  TESTS_RUN=$((TESTS_RUN + 1)); TESTS_FAIL=$((TESTS_FAIL + 1))
  printf '  FAIL %s\n' "$1"
  [[ -n "${2:-}" ]] && printf '         %s\n' "$2"
}

assert_eq()           { [[ "$2" == "$3" ]] && _pass "$1" || _fail "$1" "expected [$2] got [$3]"; }
assert_contains()     { [[ "$2" == *"$3"* ]] && _pass "$1" || _fail "$1" "[$2] lacks [$3]"; }
assert_not_contains() { [[ "$2" != *"$3"* ]] && _pass "$1" || _fail "$1" "[$2] unexpectedly contains [$3]"; }
assert_file_eq()      { local g; g=$(cat "$2" 2>/dev/null); [[ "$g" == "$3" ]] && _pass "$1" || _fail "$1" "file=[$g] != [$3]"; }
assert_no_file()      { [[ ! -e "$2" ]] && _pass "$1" || _fail "$1" "$2 exists but should not"; }
assert_file_exists()  { [[ -e "$2" ]] && _pass "$1" || _fail "$1" "$2 missing"; }

finish() {
  echo
  echo "$(basename "$0"): $TESTS_RUN run, $TESTS_FAIL failed"
  [[ $TESTS_FAIL -eq 0 ]]
}
