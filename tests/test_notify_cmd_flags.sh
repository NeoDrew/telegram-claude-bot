#!/bin/bash
# notify-cmd must never send its own flags to the chat: --help prints usage,
# a lone unknown flag fails, and ordinary text (even starting with --) sends.
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
. "$HERE/lib_assert.sh"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/scripts"; cp "$HERE/../scripts/notify-cmd" "$T/scripts/"; cp "$HERE/../scripts/lib.sh" "$T/scripts/" 2>/dev/null
printf '#!/bin/bash\necho "$*" >> "%s/sent"\n' "$T" > "$T/scripts/tg.sh"; chmod +x "$T/scripts/tg.sh" "$T/scripts/notify-cmd"
N="$T/scripts/notify-cmd"; export CHAT_ID=123

OUT=$("$N" --help 2>&1); RC=$?
assert_eq "--help exits 0" "$RC" "0"
assert_contains "--help prints usage" "$OUT" "notify-cmd --tag"
assert_no_file "--help sends nothing" "$T/sent"
OUT=$("$N" --bogus 2>&1); RC=$?
assert_eq "unknown flag exits 2" "$RC" "2"
assert_no_file "unknown flag sends nothing" "$T/sent"
"$N" "hello there" >/dev/null 2>&1
assert_contains "plain text is sent" "$(cat "$T/sent" 2>/dev/null)" "hello there"
"$N" "--- weekly report" >/dev/null 2>&1
assert_contains "text starting with dashes is sent" "$(cat "$T/sent" 2>/dev/null)" "weekly report"
finish
