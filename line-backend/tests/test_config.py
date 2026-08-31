import tempfile
import unittest
from pathlib import Path

from app.config import Settings


class SettingsTest(unittest.TestCase):
    def test_settings_support_default_values_when_env_file_is_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing_env = Path(temp_dir) / ".env"
            settings = Settings(_env_file=missing_env)

            self.assertEqual(settings.LINE_CHANNEL_SECRET, "")
            self.assertEqual(settings.LINE_CHANNEL_ACCESS_TOKEN, "")
            self.assertEqual(settings.LIFF_ID, "")
            self.assertEqual(settings.LINE_LOGIN_CHANNEL_ID, "")
            self.assertEqual(settings.APP_ENV, "development")


if __name__ == "__main__":
    unittest.main()
