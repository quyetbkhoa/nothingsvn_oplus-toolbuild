"""Pipeline must reject wrong roles and failed verification before publication."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from colornt.io import write_json
from colornt.pipeline import execute_sample, _extraction


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _fixture(self):
        base, donor, assets = [self.root / x for x in ("input-a", "input-b", "assets")]
        for path in (base, donor, assets):
            path.mkdir()
        identities = {"base": {"product_name": "A", "version_name": "A_16.1(CN01)", "android_version": "16"},
                      "donor": {"product_name": "B", "version_name": "B_16.2(EX01)", "android_version": "16"}}
        write_json(base / "firmware_info.json", identities["base"])
        write_json(donor / "firmware_info.json", identities["donor"])
        recipe = self.root / "recipe.json"
        write_json(recipe, {"input_identity": identities})
        profile = self.root / "profile.json"
        write_json(profile, {"id": "fixture", "status": "sample-verified", "reconstruction": {"recipe": "recipe.json"}})
        return base, donor, assets, recipe, profile

    def test_explicit_cache_for_other_input_is_rejected(self):
        base, donor, assets, recipe, profile = self._fixture()
        cache = self.root / "cache"
        write_json(cache / "extraction.json", {"status": "complete", "source": str(donor)})
        with self.assertRaisesRegex(ValueError, "does not belong"):
            _extraction(base, "base", self.root / "workspace", cache)

    def test_swapped_input_roles_fail_before_staging(self):
        base, donor, assets, recipe, profile = self._fixture()
        with patch("colornt.pipeline.ROOT", self.root), patch("colornt.pipeline._extraction", return_value=self.root / "cache"), \
                patch("colornt.pipeline.stage") as stager:
            with self.assertRaisesRegex(ValueError, "identity mismatch"):
                execute_sample(str(donor), str(base), profile, assets, self.root / "out")
            stager.assert_not_called()

    def test_verification_failure_cannot_be_reported_as_complete(self):
        base, donor, assets, recipe, profile = self._fixture()
        with patch("colornt.pipeline.ROOT", self.root), \
                patch("colornt.pipeline._extraction", return_value=self.root / "cache"), \
                patch("colornt.pipeline.stage"), patch("colornt.pipeline.rebuild", return_value={"partitions": []}), \
                patch("colornt.pipeline.extract"), patch("colornt.pipeline.verify", return_value={"status": "fail"}):
            with self.assertRaisesRegex(ValueError, "verification failed"):
                execute_sample(str(base), str(donor), profile, assets, self.root / "out")
        self.assertFalse((self.root / "out.pipeline.json").exists())

    def test_success_records_no_reference_requirement_or_device_claim(self):
        base, donor, assets, recipe, profile = self._fixture()
        with patch("colornt.pipeline.ROOT", self.root), \
                patch("colornt.pipeline._extraction", return_value=self.root / "cache"), \
                patch("colornt.pipeline.stage"), patch("colornt.pipeline.rebuild", return_value={"partitions": [{}]}), \
                patch("colornt.pipeline.extract"), patch("colornt.pipeline.verify", return_value={"status": "pass"}):
            result = execute_sample(str(base), str(donor), profile, assets, self.root / "out")
        self.assertEqual(result["status"], "complete")
        self.assertFalse(result["reference_required"])
        self.assertFalse(result["device_validated"])


if __name__ == "__main__":
    unittest.main()
