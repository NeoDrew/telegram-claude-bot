#!/bin/bash
# test_listener_parse.sh - exercise the REAL embedded getUpdates parser from
# listener.sh (extracted, then fed crafted JSON). Network-free: the parser is a
# pure RESPONSE-JSON -> TSV transform.
#
# TSV fields (8): update_id | chat | from_user | from_name | kind | cb_id |
#                 cb_msg_id | text
#
# Guards: the sender-name field (+ first_name/username fallback), users_shared
# / legacy user_shared, callback_query, plain text, media classification,
# edited_message, anonymous (no `from`), and the not-ok error path.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REAL="$(cd "$HERE/.." && pwd)"
. "$HERE/lib_assert.sh"

PARSER="$(mktemp)"
trap 'rm -f "$PARSER"' EXIT
# Extract the parser heredoc body (the block fed by RESPONSE="$RESPONSE").
awk '/RESPONSE="\$RESPONSE" .*python3 <<.PYEOF.$/{cap=1;next} cap&&/^PYEOF$/{cap=0} cap{print}' \
  "$REAL/scripts/listener.sh" > "$PARSER"
[[ -s "$PARSER" ]] || { echo "FAIL: could not extract parser from listener.sh"; exit 1; }

# Run the parser with a RESPONSE; show \x1f field-sep as '|'.
p() { RESPONSE="$1" python3 "$PARSER" | sed 's/\x1f/|/g'; }

echo "== listener parser tests =="

assert_eq "text: from_name = first_name" \
  "$(p '{"ok":true,"result":[{"update_id":1,"message":{"message_id":9,"chat":{"id":-100},"from":{"id":7,"first_name":"Sam"},"text":"hi"}}]}')" \
  "1|-100|7|Sam|text||9|hi"

assert_eq "text: first_name wins over username" \
  "$(p '{"ok":true,"result":[{"update_id":2,"message":{"message_id":9,"chat":{"id":-100},"from":{"id":7,"first_name":"Sam","last_name":"Lee","username":"samlee"},"text":"hi"}}]}')" \
  "2|-100|7|Sam|text||9|hi"

assert_eq "text: falls back to username when no first_name" \
  "$(p '{"ok":true,"result":[{"update_id":3,"message":{"message_id":9,"chat":{"id":-100},"from":{"id":7,"username":"test_user_1"},"text":"yo"}}]}')" \
  "3|-100|7|test_user_1|text||9|yo"

assert_eq "anonymous (no from): id 0 + empty name" \
  "$(p '{"ok":true,"result":[{"update_id":4,"message":{"message_id":9,"chat":{"id":-100},"text":"x"}}]}')" \
  "4|-100|0||text||9|x"

assert_eq "users_shared: name + uid in cb_msg slot + uname in text" \
  "$(p '{"ok":true,"result":[{"update_id":5,"message":{"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"users_shared":{"request_id":1,"users":[{"user_id":555,"username":"testuser"}]}}}]}')" \
  "5|-100|7|Alice|users_shared||555|testuser"

assert_eq "users_shared: picked user without username" \
  "$(p '{"ok":true,"result":[{"update_id":6,"message":{"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"users_shared":{"request_id":1,"users":[{"user_id":556}]}}}]}')" \
  "6|-100|7|Alice|users_shared||556|"

assert_eq "legacy singular user_shared (user_id int)" \
  "$(p '{"ok":true,"result":[{"update_id":7,"message":{"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"user_shared":{"request_id":1,"user_id":557}}}]}')" \
  "7|-100|7|Alice|users_shared||557|"

assert_eq "callback_query carries from_name too" \
  "$(p '{"ok":true,"result":[{"update_id":8,"callback_query":{"id":"c1","from":{"id":7,"first_name":"Alice"},"message":{"message_id":9,"chat":{"id":-100}},"data":"act:users add"}}]}')" \
  "8|-100|7|Alice|cb|c1|9|act:users add"

assert_eq "media photo classified, name present" \
  "$(p '{"ok":true,"result":[{"update_id":9,"message":{"message_id":9,"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"photo":[{"file_id":"x"}]}}]}')" \
  "9|-100|7|Alice|media:photo||9|x|photo_0_9.jpg|"

# A burst of photos sent together shares one timestamp; message_id keeps
# their names distinct (previously a burst overwrote itself down to a few
# files).
assert_eq "photos sharing a timestamp get distinct names" \
  "$(p '{"ok":true,"result":[{"update_id":25,"message":{"message_id":61,"date":1791247039,"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"photo":[{"file_id":"p1"}]}},{"update_id":26,"message":{"message_id":62,"date":1791247039,"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"photo":[{"file_id":"p2"}]}}]}' | cut -d'|' -f9 | tr '\n' ' ')" \
  "photo_1791247039_61.jpg photo_1791247039_62.jpg "

# Videos and other file-like media are saved to ~/Downloads like documents.
assert_eq "video without file_name gets a generated, unique name" \
  "$(p '{"ok":true,"result":[{"update_id":20,"message":{"message_id":41,"date":1791000000,"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"video":{"file_id":"v1","mime_type":"video/mp4"}}}]}')" \
  "20|-100|7|Alice|media:video||41|v1|video_1791000000_41.mp4"

assert_eq "album videos sharing a timestamp don't collide" \
  "$(p '{"ok":true,"result":[{"update_id":21,"message":{"message_id":42,"date":1791000000,"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"video":{"file_id":"v2","mime_type":"video/quicktime"}}}]}')" \
  "21|-100|7|Alice|media:video||42|v2|video_1791000000_42.mov"

assert_eq "video keeps its own file_name when present" \
  "$(p '{"ok":true,"result":[{"update_id":22,"message":{"message_id":43,"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"video":{"file_id":"v3","file_name":"clip|final.mp4"}}}]}')" \
  "22|-100|7|Alice|media:video||43|v3|clip_final.mp4"

assert_eq "voice note saved as .ogg" \
  "$(p '{"ok":true,"result":[{"update_id":23,"message":{"message_id":44,"date":5,"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"voice":{"file_id":"o1","mime_type":"audio/ogg"}}}]}')" \
  "23|-100|7|Alice|media:voice||44|o1|voice_5_44.ogg"

assert_eq "round video note gets a videonote name" \
  "$(p '{"ok":true,"result":[{"update_id":24,"message":{"message_id":45,"date":5,"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"video_note":{"file_id":"n1"}}}]}')" \
  "24|-100|7|Alice|media:video_note||45|n1|videonote_5_45.mp4"

assert_eq "edited_message parsed like a message" \
  "$(p '{"ok":true,"result":[{"update_id":10,"edited_message":{"message_id":9,"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"text":"edited"}}]}')" \
  "10|-100|7|Alice|text||9|edited"

# Reaction-triggered "edits" (Telegram sends edited_message, content unchanged,
# when someone reacts to a group message) must not re-dispatch the message.
SEEN_TMP=$(mktemp -d)
ps() { RESPONSE="$1" SEEN_FILE="$SEEN_TMP/seen.json" python3 "$PARSER" | sed 's/\x1f/|/g'; }
NOW=$(date +%s)
ps '{"ok":true,"result":[{"update_id":30,"message":{"message_id":50,"date":'"$NOW"',"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"text":"hello"}}]}' >/dev/null
assert_eq "unchanged edit (reaction) is edit_dup" \
  "$(ps '{"ok":true,"result":[{"update_id":31,"edited_message":{"message_id":50,"date":'"$NOW"',"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"text":"hello"}}]}')" \
  "31|-100|7|Alice|edit_dup||50|"
assert_eq "real edit with new text still dispatches" \
  "$(ps '{"ok":true,"result":[{"update_id":32,"edited_message":{"message_id":50,"date":'"$NOW"',"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"text":"hello there"}}]}')" \
  "32|-100|7|Alice|text||50|hello there"
assert_eq "same edit seen twice is edit_dup the second time" \
  "$(ps '{"ok":true,"result":[{"update_id":33,"edited_message":{"message_id":50,"date":'"$NOW"',"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"text":"hello there"}}]}')" \
  "33|-100|7|Alice|edit_dup||50|"
assert_eq "edit of an old, never-seen message is edit_dup" \
  "$(ps '{"ok":true,"result":[{"update_id":34,"edited_message":{"message_id":60,"date":'"$((NOW - 86400))"',"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"text":"days old"}}]}')" \
  "34|-100|7|Alice|edit_dup||60|"
rm -rf "$SEEN_TMP"

assert_contains "not-ok response yields ERR" \
  "$(p '{"ok":false,"description":"boom"}')" "ERR"

# newline in text becomes the record-sep \x1e (the loop turns it back into \n)
assert_contains "newline in text becomes \\x1e not a raw newline" \
  "$(RESPONSE='{"ok":true,"result":[{"update_id":11,"message":{"message_id":1,"chat":{"id":-100},"from":{"id":7,"first_name":"Alice"},"text":"a\nb"}}]}' python3 "$PARSER" | sed 's/\x1e/~/g')" \
  "a~b"

finish
