"""Verification test suite for Jar standalone appliance."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path


class TestJarAppliance(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.jar_home = Path(self.temp_dir.name)
        os.environ["JAR_HOME"] = str(self.jar_home)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_hardware_profiling(self) -> None:
        from jar.hardware import inspect_all
        telemetry = inspect_all()
        self.assertIn("platform", telemetry)
        self.assertIn("memory", telemetry)
        self.assertIn("accelerators", telemetry)
        self.assertIn("recommendation", telemetry)

        rec = telemetry["recommendation"]
        self.assertIn(rec["tier"], ("minimal", "light", "standard", "heavy"))
        self.assertIn(rec["backend"], ("metal", "cuda", "cpu"))
        self.assertGreater(rec["recommended_context"], 0)

    def test_setup_url_generation(self) -> None:
        from jar.server import get_setup_urls
        local_url, net_url = get_setup_urls(port=7788)
        self.assertTrue(local_url.startswith("http://localhost:7788/setup#k="))
        self.assertTrue(net_url.startswith("http://") and ":7788/setup#k=" in net_url)

        # Verify key was written to setup directory
        key_file = self.jar_home / "config" / "box-setup" / "setup-key"
        self.assertTrue(key_file.exists())
        self.assertGreater(len(key_file.read_text().strip()), 16)


if __name__ == "__main__":
    unittest.main()

