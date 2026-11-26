import unittest
from unittest.mock import patch

from voice import listen


class ListenTests(unittest.TestCase):
    def test_listen_passes_cue_and_model_to_recording_and_transcription(self):
        with patch("voice.record_audio") as record, patch("voice.transcribe", return_value=("ok", 1.0)) as transcribe:
            self.assertEqual(listen(4, "base", cue=False), ("ok", 1.0))
            record.assert_called_once_with(4, cue=False)
            transcribe.assert_called_once_with(record.return_value, "base")

    def test_listen_keeps_countdown_by_default(self):
        with patch("voice.record_audio") as record, patch("voice.transcribe", return_value=("ok", 1.0)):
            listen(6)
            record.assert_called_once_with(6, cue=True)


if __name__ == "__main__":
    unittest.main()
