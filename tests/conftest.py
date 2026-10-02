import os
import tempfile

# Before anything imports twitch_radio.config: keep tests away from the real .env and data/.
os.environ["TWITCH_RADIO_HOME"] = tempfile.mkdtemp(prefix="twitch-radio-tests-")
os.environ.pop("TWITCH_RADIO_NO_YTDLP_OVERRIDE", None)
