"""Regression coverage for preserving Android links, source selection and metadata."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from colornt.android_links import MAGIC, copy_entry, entry_digest, link_target
from colornt.compare import three_way_images, three_way_trees
from colornt.io import write_json
from colornt.reconstruct import stage, verify, _validate_recipe, _sparse, import_assets, verify_assets
from colornt.images import expand_sparse, inspect_image


class ReconstructionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def _fixture(self):
        roots = [self.root / x for x in ("base", "donor", "assets")]
        a, b, assets = roots
        for root in roots:
            (root / "vendor").mkdir(parents=True)
            (root / "system").mkdir()
        (a / "vendor/driver").write_bytes(b"hardware-A")
        (b / "vendor/driver").write_bytes(b"hardware-B")
        (a / "system/os").write_bytes(b"OS-A")
        (b / "system/os").write_bytes(b"OS-B")
        (b / "system/bin").write_bytes(MAGIC + ("/system/bin\0").encode("utf-16-le"))
        (assets / "mod").write_bytes(b"mod")
        (assets / "config").mkdir()
        entries = [
            {"path": "vendor/driver", "source": "base", "source_path": "vendor/driver"},
            {"path": "system/os", "source": "donor", "source_path": "system/os"},
            {"path": "system/bin", "source": "donor", "source_path": "system/bin", "link_target": "/system/bin"},
            {"path": "system/app/mod", "source": "assets", "source_path": "mod"}]
        source_map = dict(zip(("base", "donor", "assets"), roots))
        for entry in entries:
            source = source_map[entry["source"]] / entry["source_path"]
            entry.update(sha256=entry_digest(source), size=source.stat().st_size, mtime_ns=1700000000000000000)
        metadata = []
        for name in ("system", "vendor"):
            for suffix in ("fs_config", "file_contexts"):
                relative = f"config/{name}_{suffix}"
                source = assets / relative
                source.write_text("metadata\n", encoding="utf-8")
                metadata.append({"path": relative, "asset": relative, "sha256": entry_digest(source)})
        recipe = {"schema": 1, "kind": "sample-reconstruction", "id": "fixture", "files": entries,
                  "directories": [{"path": x, "mtime_ns": 1700000000000000000}
                                  for x in ("system", "system/app", "system/empty", "vendor")],
                  "metadata": metadata,
                  "images": [{"partition": x, "kind": "repack-erofs", "output": f"system/{x}.img"}
                             for x in ("system", "vendor")]}
        profile = self.root / "recipe.json"
        write_json(profile, recipe)
        return roots, profile, recipe

    def _stage(self, roots, recipe, out):
        # No native tools needed for synthetic content tests on Windows CI.
        with patch("colornt.reconstruct.android_directory", side_effect=lambda p: p.mkdir(parents=True)):
            return stage(*roots, recipe, out)

    def test_replay_uses_target_hardware_donor_os_and_preserves_link(self):
        roots, profile, recipe = self._fixture()
        out = self.root / "out"
        report = self._stage(roots, profile, out)
        self.assertEqual(report["status"], "complete")
        self.assertFalse(report["reference_required"])
        self.assertEqual((out / "vendor/driver").read_bytes(), b"hardware-A")
        self.assertEqual((out / "system/os").read_bytes(), b"OS-B")
        self.assertEqual(link_target(out / "system/bin"), "/system/bin")
        self.assertTrue((out / "system/empty").is_dir())
        self.assertEqual((roots[1] / "vendor/driver").read_bytes(), b"hardware-B")

    def test_corrupt_mod_fails_before_workspace_creation(self):
        roots, profile, recipe = self._fixture()
        (roots[2] / "mod").write_bytes(b"bad")
        out = self.root / "out"
        with self.assertRaisesRegex(ValueError, "asset checksum"):
            self._stage(roots, profile, out)
        self.assertFalse(out.exists())

    def test_partial_source_partition_can_be_completed_without_mutating_old_cache(self):
        roots, profile, recipe = self._fixture()
        extra = self.root / 'extra'; (extra / 'system').mkdir(parents=True)
        for name in ('os', 'bin'):
            copy_entry(roots[1] / 'system' / name, extra / 'system' / name)
            (roots[1] / 'system' / name).unlink()
        overrides = {'donor': {'system': extra}}
        out = self.root / 'completed'
        with patch('colornt.reconstruct.android_directory', side_effect=lambda p: p.mkdir(parents=True)):
            result = stage(*roots, profile, out, source_overrides=overrides)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual((out / 'system/os').read_bytes(), b'OS-B')
        self.assertFalse((roots[1] / 'system/os').exists())
        (extra / 'system/os').write_bytes(b'OS-X')
        with patch('colornt.reconstruct.android_directory', side_effect=lambda p: p.mkdir(parents=True)):
            with self.assertRaisesRegex(ValueError, 'differs from sample'):
                stage(*roots, profile, self.root / 'bad', source_overrides=overrides)

    def test_source_override_cannot_overlap_the_output_or_use_unknown_roles(self):
        roots, profile, recipe = self._fixture()
        with self.assertRaisesRegex(ValueError, 'Unknown reconstruction source override'):
            stage(*roots, profile, self.root / 'out', source_overrides={'assets': {'system': roots[1]}})
        with self.assertRaisesRegex(ValueError, 'overlaps'):
            stage(*roots, profile, roots[1] / 'out', source_overrides={'donor': {'system': roots[1]}})

    def test_asset_verification_detects_missing_corrupt_and_unexpected_files(self):
        roots, profile, recipe = self._fixture()
        self.assertEqual(verify_assets(profile, roots[2])["status"], "pass")
        (roots[2] / "mod").write_bytes(b"bad")
        (roots[2] / "config/system_fs_config").unlink()
        (roots[2] / "extra").write_bytes(b"extra")
        result = verify_assets(profile, roots[2])
        self.assertEqual(result["status"], "fail")
        self.assertEqual({x["reason"] for x in result["issues"]},
                         {"content/link mismatch", "missing asset", "unexpected asset"})

    def test_quick_asset_check_does_not_claim_digest_validation(self):
        roots, profile, recipe = self._fixture()
        (roots[2] / "mod").write_bytes(b"bad")  # Same size; only full check detects it.
        quick = verify_assets(profile, roots[2], content=False)
        self.assertEqual(quick["status"], "pass")
        self.assertFalse(quick["content_verified"])
        self.assertEqual(verify_assets(profile, roots[2])["status"], "fail")

    def test_empty_asset_directory_blocks_sample_build_plan(self):
        from colornt.build import plan
        roots, profile, recipe = self._fixture()
        empty = self.root / "empty-assets"
        empty.mkdir()
        config = self.root / "profile.json"
        write_json(config, {"id": "test", "status": "sample-verified", "requires_donor": True,
                            "variant": {"install_mods": True, "install_toolbox": True},
                            "partitions": [{"name": "system"}], "mods_complete": True, "toolbox_complete": True,
                            "reconstruction": {"recipe": str(profile), "assets": str(empty)}})
        result = plan(str(roots[0]), str(roots[1]), config, "fastboot")
        self.assertFalse(result["ready"])
        self.assertTrue(any("incomplete" in x for x in result["blockers"]))

    def test_wrong_same_size_os_fails_instead_of_silently_building_stock(self):
        roots, profile, recipe = self._fixture()
        (roots[1] / "system/os").write_bytes(b"OS-X")
        out = self.root / "out"
        with self.assertRaisesRegex(ValueError, "differs from sample"):
            self._stage(roots, profile, out)
        self.assertEqual(__import__("colornt.io", fromlist=["read_json"]).read_json(self.root / "out.stage.json")["status"], "failed")

    def test_verify_detects_unexpected_files_and_metadata_changes(self):
        roots, profile, recipe = self._fixture()
        out = self.root / "stage"
        unpacked = self.root / "unpacked"
        self._stage(roots, profile, out)
        self._stage(roots, profile, unpacked)
        self.assertEqual(verify(out, unpacked, profile)["status"], "pass")
        (unpacked / "system/unexpected").write_bytes(b"extra")
        (unpacked / "config/system_fs_config").write_text("different\n")
        result = verify(out, unpacked, profile)
        self.assertEqual(result["status"], "fail")
        self.assertEqual({x["reason"] for x in result["issues"]}, {"unexpected file", "Android metadata differs"})

    def test_recipe_rejects_duplicate_but_allows_distinct_android_case(self):
        roots, profile, recipe = self._fixture()
        recipe["files"].append(dict(recipe["files"][0]))
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            _validate_recipe(recipe)
        recipe["files"][-1]["path"] = "vendor/Driver"
        _validate_recipe(recipe)

    def test_link_representation_hash_is_cross_platform(self):
        source = self.root / "link"
        source.write_bytes(MAGIC + ("../target\0").encode("utf-16-le"))
        self.assertEqual(entry_digest(source), hashlib.sha256(b"../target").hexdigest())
        destination = self.root / "copied"
        copy_entry(source, destination)
        self.assertEqual(link_target(destination), "../target")
        self.assertEqual(entry_digest(source), entry_digest(destination))

    def test_sparse_repack_round_trip(self):
        raw, sparse, expanded = [self.root / x for x in ("raw", "sparse", "expanded")]
        raw.write_bytes(b"a" * 4096 + b"\0" * 4096)
        _sparse(raw, sparse)
        expand_sparse(sparse, expanded)
        self.assertEqual(raw.read_bytes() + b"\0" * 4096, expanded.read_bytes())

    def test_small_sparse_fits_fastbootd_download_size_guard(self):
        raw, sparse, expanded = [self.root / x for x in ("small-raw", "small-sparse", "small-expanded")]
        raw.write_bytes(b"a" * 4096)
        _sparse(raw, sparse)
        info = inspect_image(sparse)
        self.assertLessEqual(info["size"], info["expanded_size"])
        self.assertEqual(info["expanded_size"], 8192)
        expand_sparse(sparse, expanded)
        self.assertEqual(expanded.read_bytes()[:4096], raw.read_bytes())
        self.assertEqual(expanded.read_bytes()[4096:], b"\0" * 4096)

    def test_fresh_checkout_asset_import_and_corrupt_reference_preflight(self):
        roots, profile, recipe = self._fixture()
        reference = self.root / "reference"
        self._stage(roots, profile, reference)
        images = self.root / "images"
        images.mkdir()
        recovered = self.root / "imported"
        with patch("colornt.reconstruct.android_directory", side_effect=lambda p: p.mkdir(parents=True)):
            report = import_assets(profile, reference, images, recovered)
        self.assertEqual(report["status"], "imported")
        self.assertEqual((recovered / "mod").read_bytes(), b"mod")
        self.assertEqual(report["files"], 5)
        (reference / "system/app/mod").write_bytes(b"bad")
        bad = self.root / "bad-import"
        with self.assertRaisesRegex(ValueError, "mod asset differs"):
            import_assets(profile, reference, images, bad)
        self.assertFalse(bad.exists())

    def test_byte_three_way_attributes_changes_and_excludes_config(self):
        roots = [self.root / x for x in ("a", "b", "c")]
        for root in roots:
            (root / "system").mkdir(parents=True)
            (root / "config").mkdir()
            (root / "config/different").write_text(str(root))
        a, b, c = roots
        for name, aa, bb, cc in (("base", b"a", b"b", b"a"), ("donor", b"a", b"b", b"b"),
                                 ("both", b"a", b"a", b"a"), ("modified", b"a", b"b", b"c"),
                                 ("removed", b"a", None, None), ("added", None, None, b"a")):
            for root, content in zip(roots, (aa, bb, cc)):
                if content is not None:
                    (root / "system" / name).write_bytes(content)
        result = three_way_trees(*roots)
        self.assertEqual(result["counts"], {x: 1 for x in ("base", "donor", "both", "modified", "absent-in-output", "added")})

    def test_image_comparison_normalizes_layout_and_rejects_ambiguity(self):
        roots = [self.root / x for x in ("a", "b", "c")]
        for root, group in zip(roots, ("BOOTLOADER", "SYSTEM", "firmware-update")):
            (root / group).mkdir(parents=True)
            (root / group / "boot.img").write_bytes(b"A" if root != roots[1] else b"B")
        result = three_way_images(*roots)
        self.assertEqual(result["counts"], {"base": 1})
        (roots[0] / "boot.img").write_bytes(b"duplicate")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            three_way_images(*roots)


if __name__ == "__main__":
    unittest.main()
