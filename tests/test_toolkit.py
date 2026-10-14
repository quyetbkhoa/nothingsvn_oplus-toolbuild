import hashlib
import io
import json
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from colornt.build import build_images, package, plan
from colornt.compare import compare_trees
from colornt.evidence import parse_log, sanitize
from colornt.images import expand_sparse, inspect_image
from colornt.inventory import inventory, three_way
from colornt.io import confined, is_zip_container, write_json
from colornt.recipes import apply_recipe


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ToolkitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_sparse_raw_fill_hole_and_crc(self):
        source = self.root / "sparse.img"
        target = self.root / "expanded.img"
        chunks = [struct.pack("<HHII", 0xCAC1, 0, 1, 16) + b"abcd",
                  struct.pack("<HHII", 0xCAC2, 0, 2, 16) + b"1234",
                  struct.pack("<HHII", 0xCAC3, 0, 1, 12),
                  struct.pack("<HHII", 0xCAC4, 0, 0, 16) + b"\0" * 4]
        source.write_bytes(struct.pack("<I4H4I", 0xED26FF3A, 1, 0, 28, 12, 4, 4, 4, 0) + b"".join(chunks))
        self.assertEqual(inspect_image(source)["expanded_size"], 16)
        expand_sparse(source, target)
        self.assertEqual(target.read_bytes(), b"abcd12341234" + b"\0" * 4)

    def test_sparse_truncation_leaves_no_output(self):
        source = self.root / "sparse.img"
        source.write_bytes(struct.pack("<I4H4I", 0xED26FF3A, 1, 0, 28, 12, 4, 1, 1, 0) +
                           struct.pack("<HHII", 0xCAC1, 0, 1, 16) + b"ab")
        target = self.root / "expanded.img"
        with self.assertRaisesRegex(ValueError, "Truncated"):
            expand_sparse(source, target)
        self.assertFalse(target.exists())
        self.assertFalse(target.with_name(target.name + ".partial").exists())

    def test_sparse_rejects_block_count_mismatch(self):
        source = self.root / "sparse.img"
        source.write_bytes(struct.pack("<I4H4I", 0xED26FF3A, 1, 0, 28, 12, 4, 2, 1, 0) +
                           struct.pack("<HHII", 0xCAC3, 0, 1, 12))
        with self.assertRaisesRegex(ValueError, "block count"):
            expand_sparse(source, self.root / "expanded.img")

    def test_confinement_rejects_windows_and_posix_traversal(self):
        for path in ("../escape", "/outside", "C:\\outside", "part/../../out", "..\\outside"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                confined(self.root, path)
        self.assertEqual(confined(self.root, "system/file"), self.root / "system" / "file")

    def test_recipe_preflight_is_atomic_for_bad_asset(self):
        tree, assets = self.root / "tree", self.root / "assets"
        tree.mkdir()
        assets.mkdir()
        (tree / "first").write_bytes(b"first")
        (tree / "second").write_bytes(b"second")
        (assets / "new").write_bytes(b"new")
        recipe = {"operations": [
            {"kind": "replace", "path": "first", "asset": "new", "before_sha256": digest(b"first"), "after_sha256": digest(b"new")},
            {"kind": "replace", "path": "second", "asset": "new", "before_sha256": digest(b"second"), "after_sha256": "bad"}]}
        with self.assertRaisesRegex(ValueError, "Asset hash"):
            apply_recipe(tree, assets, recipe)
        self.assertEqual((tree / "first").read_bytes(), b"first")

    def test_recipe_add_replace_delete_and_text(self):
        tree, assets = self.root / "tree", self.root / "assets"
        tree.mkdir()
        assets.mkdir()
        (tree / "prop").write_bytes(b"key=old\n")
        (tree / "remove").write_bytes(b"remove")
        (assets / "app").write_bytes(b"app")
        recipe = {"operations": [
            {"kind": "text-replace", "path": "prop", "old": "old", "new": "new", "before_sha256": digest(b"key=old\n"), "after_sha256": digest(b"key=new\n")},
            {"kind": "delete", "path": "remove", "before_sha256": digest(b"remove")},
            {"kind": "replace", "path": "new/app", "asset": "app", "before_sha256": None, "after_sha256": digest(b"app")}]}
        report = apply_recipe(tree, assets, recipe)
        self.assertEqual(len(report), 3)
        self.assertEqual((tree / "prop").read_bytes(), b"key=new\n")
        self.assertFalse((tree / "remove").exists())
        self.assertEqual((tree / "new/app").read_bytes(), b"app")

    def test_recipe_rejects_duplicate_target(self):
        tree, assets = self.root / "tree", self.root / "assets"
        tree.mkdir()
        assets.mkdir()
        (assets / "app").write_bytes(b"app")
        op = {"kind": "replace", "path": "app", "asset": "app", "before_sha256": None, "after_sha256": digest(b"app")}
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            apply_recipe(tree, assets, {"operations": [op, op]})
        self.assertFalse((tree / "app").exists())

    def test_embedded_zip_is_not_rom_container(self):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as archive:
            archive.writestr("cert.pem", "certificate")
        path = self.root / "my_engineering.img"
        path.write_bytes(b"not-a-zip-filesystem" * 100 + stream.getvalue())
        self.assertTrue(zipfile.is_zipfile(path))
        self.assertFalse(is_zip_container(path))
        self.assertEqual(inventory(path, hashes=False)["kind"], "image")

    def test_no_hash_inventory_reads_zip_layout_only(self):
        path = self.root / "rom.zip"
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("payload.bin", b"CrAU" + b"\0" * 12)
        result = inventory(path, hashes=False)
        self.assertEqual(result["kind"], "zip")
        self.assertNotIn("sha256", result)
        self.assertEqual(result["entries"][0]["path"], "payload.bin")

    def test_three_way_detects_content_origins(self):
        folders = [self.root / name for name in ("base", "donor", "output")]
        for folder in folders:
            folder.mkdir()
        a, b, c = folders
        for folder in folders:
            (folder / "same").write_bytes(b"same")
        (a / "hardware").write_bytes(b"A")
        (b / "hardware").write_bytes(b"B")
        (c / "hardware").write_bytes(b"A")
        (b / "donor").write_bytes(b"OS")
        (c / "donor").write_bytes(b"OS")
        (c / "added").write_bytes(b"new")
        result = three_way(*(inventory(folder) for folder in folders))
        self.assertEqual(result["counts"], {"modified-or-added": 1, "donor": 1, "base": 1, "both": 1})

    def test_byte_diff_ignores_timestamps_but_not_content(self):
        a, b = self.root / "a", self.root / "b"
        a.mkdir()
        b.mkdir()
        (a / "file").write_bytes(b"abc")
        (b / "file").write_bytes(b"abd")
        result = compare_trees(a, b)
        self.assertEqual(result["counts"], {"changed": 1})

    def test_log_records_patch_targets_without_claiming_semantics(self):
        log = """2026-10-09T14:00:00Z [DEBLOAT] - Removing directory: build/portrom/images/my_product/app/Maps
2026-10-09T14:00:00Z [*] Target   : framework.jar
2026-10-09T14:00:00Z [1/1] android/content/Intent.smali::getIntExtra(Ljava/lang/String;I)I
2026-10-09T14:00:00Z [INFO] - Launcher is A17-only; skipping
"""
        result = parse_log(log)
        self.assertEqual(result["observed_debloat_paths"], ["my_product/app/Maps"])
        self.assertEqual(len(result["observed_framework_methods"]["framework.jar"]), 1)
        self.assertEqual(len(result["skipped"]), 1)
        self.assertTrue(result["limitations"])

    def test_signed_urls_are_redacted(self):
        clean = sanitize("url https://user:pass@example.com/ota.zip?Signature=secret&token=other next")
        self.assertNotIn("secret", clean)
        self.assertNotIn("user:pass", clean)
        self.assertIn("https://example.com/ota.zip?REDACTED", clean)

    def _fixture_profile(self):
        base, donor, assets = [self.root / x for x in ("base", "donor", "assets")]
        for path in (base, donor, assets):
            path.mkdir()
        (base / "boot.img").write_bytes(b"base-kernel")
        (donor / "system.img").write_bytes(b"donor-system")
        config = {"id": "synthetic", "status": "verified", "requires_donor": True,
                  "variant": {"install_mods": False, "install_toolbox": False},
                  "mods_complete": False, "toolbox_complete": False,
                  "partitions": [
                      {"source": "base", "path": "boot.img", "output": "firmware-update/boot.img", "sha256": digest(b"base-kernel")},
                      {"source": "donor", "path": "system.img", "output": "system/system.img", "sha256": digest(b"donor-system")}],
                  "install_metadata": {"Devices": {"Model": "TEST"}, "ROM": {"Android": "16"}}}
        profile = self.root / "profile.json"
        write_json(profile, config)
        return base, donor, assets, profile

    def test_pinned_image_build_and_zip_pipeline(self):
        base, donor, assets, profile = self._fixture_profile()
        out = self.root / "out"
        report = build_images(base, donor, profile, assets, out, False, False)
        self.assertEqual(report["status"], "complete")
        self.assertEqual((out / "firmware-update/boot.img").read_bytes(), b"base-kernel")
        package_report = package(out, self.root / "package.zip")
        self.assertFalse(package_report["device_validated"])
        with zipfile.ZipFile(self.root / "package.zip") as archive:
            self.assertEqual(archive.read("system/system.img"), b"donor-system")
        self.assertEqual((base / "boot.img").read_bytes(), b"base-kernel")

    def test_build_bad_input_fails_before_creating_output(self):
        base, donor, assets, profile = self._fixture_profile()
        (base / "boot.img").write_bytes(b"wrong")
        out = self.root / "out"
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            build_images(base, donor, profile, assets, out, False, False)
        self.assertFalse(out.exists())

    def test_build_draft_is_blocked_and_flags_are_not_ignored(self):
        base, donor, assets, profile = self._fixture_profile()
        flags = plan(str(base), str(donor), profile, "fastboot", True, False)
        self.assertFalse(flags["ready"])
        self.assertTrue(any("flag combination" in x for x in flags["blockers"]))
        config = json.loads(profile.read_text())
        config["status"] = "draft"
        write_json(profile, config)
        with self.assertRaisesRegex(ValueError, "not verified"):
            build_images(base, donor, profile, assets, self.root / "out", False, False)

    def test_ota_cannot_silently_fall_back_to_fastboot(self):
        base, donor, assets, profile = self._fixture_profile()
        result = plan(str(base), str(donor), profile, "ota", False, False)
        self.assertFalse(result["ready"])
        self.assertTrue(any("OTA" in x for x in result["blockers"]))


if __name__ == "__main__":
    unittest.main()
