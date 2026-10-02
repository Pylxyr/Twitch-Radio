import sys

# The packaged core launches itself as the yt-dlp worker (a frozen exe has no
# `python -m`). Decide that before importing anything heavy: a worker only
# needs the standard library until it actually extracts.
if "--extractor-worker" in sys.argv[1:]:
    from twitch_radio.extractor_worker import main as _worker_main

    sys.exit(_worker_main())

from twitch_radio.bot import run

if __name__ == "__main__":
    run()
