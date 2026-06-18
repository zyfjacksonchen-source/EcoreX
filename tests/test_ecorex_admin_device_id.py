import importlib.util
import pathlib
import unittest
from urllib.parse import quote


def load_admin_api():
    module_path = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "ecorex-admin-api" / "ecorex_admin_api.py"
    spec = importlib.util.spec_from_file_location("ecorex_admin_api", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


admin_api = load_admin_api()


class DeviceIdMatchesTest(unittest.TestCase):
    def test_matches_raw_and_url_encoded_device_ids(self):
        raw = "\u7535\u8111-\u5f20\u4e09-darwin"
        encoded = quote(raw, safe="")
        self.assertTrue(admin_api.device_id_matches(raw, raw))
        self.assertTrue(admin_api.device_id_matches(raw, encoded))
        self.assertTrue(admin_api.device_id_matches(encoded, raw))

    def test_empty_device_id_keeps_legacy_sessions_compatible(self):
        self.assertTrue(admin_api.device_id_matches("", "ecorex-device"))
        self.assertTrue(admin_api.device_id_matches("ecorex-device", ""))

    def test_different_devices_do_not_match(self):
        self.assertFalse(admin_api.device_id_matches("ecorex-device-a", "ecorex-device-b"))


if __name__ == "__main__":
    unittest.main()
