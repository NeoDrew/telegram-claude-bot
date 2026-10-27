#!/usr/bin/env python3
"""
E2E: videos (and voice/audio/GIF/video-note messages) sent to the bot are
saved to ~/Downloads like documents, through the REAL listener.sh and tg.sh.

Previously only documents and photos were downloaded; everything else got
"Got your video, but I only understand text" and was dropped.

HOME is pointed at a throwaway directory so nothing touches the real
~/Downloads. Asserts:
  1. a nameless video lands in ~/Downloads under a generated name, with the
     fake file's exact bytes, and a "Saved" confirmation is sent;
  2. a voice note is saved the same way;
  3. a video over the 20 MB bot-download limit gets a plain-English
     explanation instead of the raw API error, and no stray file.

Run: python3 tests/e2e/test_media_download.py
"""

import html
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from harness import E2EHarness, OWNER_USER_ID  # noqa: E402

CHAT = int(OWNER_USER_ID)


def media_update(h, kind, payload, message_id):
    update = {"message": {
        "message_id": message_id,
        "date": 1791000000,
        "chat": {"id": CHAT, "type": "private"},
        "from": {"id": CHAT, "first_name": "Alice", "is_bot": False},
        kind: payload,
    }}
    return h._post_json("/__test__/inject_update", update)


def wait_file(path, timeout=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.2)
    return False


def main():
    home = tempfile.mkdtemp(prefix="tcbot-e2e-home-")
    downloads = os.path.join(home, "Downloads")
    os.makedirs(downloads)
    h = E2EHarness(label="media-download")
    try:
        h.setup(extra_env={"HOME": home, "BOT_DRAFT_STREAMING": "0"})
        h.start_listener()

        # 1. Nameless video, the usual shape from a phone camera roll.
        media_update(h, "video", {"file_id": "vid1", "mime_type": "video/mp4"}, 501)
        saved = os.path.join(downloads, "video_1791000000_501.mp4")
        assert wait_file(saved), f"video was not saved to {saved}"
        with open(saved, "rb") as f:
            assert f.read() == b"FAKEFILE:files/vid1.bin", "saved bytes don't match the download"
        h.wait_for_call("sendMessage",
                        lambda p: "Saved" in p.get("text", "")
                        and "video_1791000000_501.mp4" in p.get("text", ""),
                        timeout=10)
        print("ok: video saved to ~/Downloads with the right bytes, confirmation sent")

        # 2. Voice note.
        media_update(h, "voice", {"file_id": "voc1", "mime_type": "audio/ogg"}, 502)
        assert wait_file(os.path.join(downloads, "voice_1791000000_502.ogg")), \
            "voice note was not saved"
        print("ok: voice note saved")

        # 3. Over the 20 MB cloud-API limit.
        media_update(h, "video", {"file_id": "too_big", "mime_type": "video/mp4"}, 503)
        call = h.wait_for_call("sendMessage",
                               lambda p: "20 MB" in p.get("text", ""), timeout=10)
        # Outbound text is Telegram HTML, so the apostrophe arrives escaped.
        assert "Couldn't save" in html.unescape(call["params"]["text"])
        assert not os.path.exists(os.path.join(downloads, "video_1791000000_503.mp4")), \
            "a failed download left a stray file behind"
        print("ok: oversized video gets a plain-English 20 MB explanation, no stray file")

        # 4. Photo burst: several photos in the same second (one album) must
        # each get their own file. Naming by the second alone collapsed a
        # larger burst into far fewer files.
        for i, mid in enumerate((601, 602, 603)):
            media_update(h, "photo", [{"file_id": f"ph{i}"}], mid)
        burst = [os.path.join(downloads, f"photo_1791000000_{m}.jpg") for m in (601, 602, 603)]
        assert all(wait_file(p) for p in burst), \
            f"photo burst did not produce 3 files: {sorted(os.listdir(downloads))}"
        contents = set()
        for p in burst:
            with open(p, "rb") as f:
                contents.add(f.read())
        assert len(contents) == 3, "burst photos overwrote each other"
        print("ok: a 3-photo burst in the same second saves 3 distinct files")

        # 4b. Every photo of the album reaches Claude, uncaptioned ones too,
        # as ONE message (the burst debounce coalesces them), carrying the
        # saved paths for Claude's Read tool.
        inbox = os.path.join(h.room_dir(str(CHAT)), "inbox.ndjson")
        deadline = time.time() + 15
        line = ""
        while time.time() < deadline:
            if os.path.exists(inbox):
                lines = [l for l in open(inbox).read().splitlines() if "photo_1791000000_601" in l]
                if lines:
                    line = lines[0]
                    break
            time.sleep(0.2)
        assert line, "the album never reached Claude's inbox"
        assert all(f"photo_1791000000_{m}.jpg" in line for m in (601, 602, 603)), \
            f"the album was not coalesced into one message: {line[:300]}"
        assert "Read tool" in line
        print("ok: the whole album reaches Claude as one message with every saved path")

        # 5. Two documents with the same name: the second must not overwrite
        # the first.
        media_update(h, "document", {"file_id": "docA", "file_name": "report.pdf"}, 701)
        assert wait_file(os.path.join(downloads, "report.pdf"))
        media_update(h, "document", {"file_id": "docB", "file_name": "report.pdf"}, 702)
        assert wait_file(os.path.join(downloads, "report_1.pdf")), \
            "second same-named document did not get a non-clobbering name"
        with open(os.path.join(downloads, "report.pdf"), "rb") as f:
            assert f.read() == b"FAKEFILE:files/docA.bin", "first document was overwritten"
        print("ok: a same-named document is saved as report_1.pdf, original untouched")

        texts = [c["params"].get("text", "") for c in h.all_calls("sendMessage")]
        assert not any("only understand text" in t for t in texts), \
            "a media message still hit the text-only fallback"
        print("ok: no 'I only understand text' replies")

    except Exception:
        import traceback
        traceback.print_exc()
        h.keep_sandbox = True
        sys.exit(1)
    finally:
        h.teardown()
        shutil.rmtree(home, ignore_errors=True)

    print("\nok: videos and other media download to ~/Downloads like documents")


if __name__ == "__main__":
    main()
