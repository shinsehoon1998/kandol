import unittest
import json
import os
import sys
from pathlib import Path

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from web.app import app

class TestEdmsSettings(unittest.TestCase):
    def setUp(self):
        self.app = app.test_client()
        self.config_path = ROOT / "edms_config.json"
        
        # Backup existing config if any
        self.backup_path = ROOT / "edms_config.json.bak"
        if self.config_path.exists():
            if self.backup_path.exists():
                os.remove(self.backup_path)
            os.rename(self.config_path, self.backup_path)

    def tearDown(self):
        # Restore backup
        if self.config_path.exists():
            os.remove(self.config_path)
        if self.backup_path.exists():
            os.rename(self.backup_path, self.config_path)

    def test_get_config_creates_default(self):
        # When config file doesn't exist, GET should create it with defaults
        response = self.app.get('/edms/config')
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.data)
        
        self.assertIn("delays", data)
        self.assertIn("offsets", data)
        self.assertIn("ratios", data)
        self.assertEqual(data["offsets"]["image_add_x"], 1117)
        self.assertTrue(self.config_path.exists())

    def test_post_config_saves_correctly(self):
        # Save custom config via POST
        custom_data = {
            "delays": {
                "dialog_open_wait": 2.5,
                "tab_click_wait": 3.0
            },
            "offsets": {
                "image_add_x": 1200,
                "image_add_y": 300
            },
            "ratios": {
                "pop_send_x": 0.5
            }
        }
        
        response = self.app.post('/edms/config', 
                                 data=json.dumps(custom_data), 
                                 content_type='application/json')
        self.assertEqual(response.status_code, 200)
        
        # Verify JSON response
        res_data = json.loads(response.data)
        self.assertTrue(res_data["success"])
        self.assertEqual(res_data["config"]["delays"]["dialog_open_wait"], 2.5)
        self.assertEqual(res_data["config"]["offsets"]["image_add_x"], 1200)
        
        # Verify actual file writing
        with open(self.config_path, "r", encoding="utf-8") as f:
            file_data = json.load(f)
        self.assertEqual(file_data["delays"]["dialog_open_wait"], 2.5)
        self.assertEqual(file_data["offsets"]["image_add_x"], 1200)
        # Verify default fallback logic inside app.py: it merges missing keys
        self.assertEqual(file_data["offsets"]["select_all_x"], 373)

    def test_calibrate_fails_gracefully_when_no_window(self):
        # Since EDMS window is not open, calibration should fail with clear message
        response = self.app.post('/edms/calibrate')
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.data)
        self.assertFalse(data["success"])
        self.assertIn("EDMS 팝업 창을 찾을 수 없습니다", data["error"])

if __name__ == '__main__':
    unittest.main()
