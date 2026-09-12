"""Offline regression tests; all Unity writes target temporary fixtures."""
import argparse
import base64
import contextlib
import hashlib
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import unity_export as ue


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScLbtAAAAABJRU5ErkJggg==")
LEGACY = b'''public static class DropByScenePropImporter {
        public static int ImportNewProps()
        {
            foreach (var glbPath in glbs)
            {
                string id = SanitizeId(Path.GetFileNameWithoutExtension(glbPath));
            }
            return 0;
        }
        public static int ForceRebuildAllProps() { return 0; }
}'''
GENERATOR = b'''private static bool IsSourceCutoutIcon(PlaceableItemData item)
        {
            if (item?.icon == null) return false;
            var path = AssetDatabase.GetAssetPath(item.icon);
            return path.StartsWith(DropByScenePropImporter.SourceIconFolder);
        }
        private static string SafeFileName(string value) { return value; }
'''


def make_glb(payload=None):
    payload = payload or {"asset": {"version": "2.0"}, "scenes": [{"nodes": []}]}
    body = json.dumps(payload).encode("utf-8")
    body += b" " * ((-len(body)) % 4)
    return struct.pack("<4sII", b"glTF", 2, 20 + len(body)) + struct.pack("<I4s", len(body), b"JSON") + body


class UnityExportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / "Unity fixture"
        (self.project / "Assets").mkdir(parents=True)
        (self.project / "ProjectSettings").mkdir()
        self.work = self.root / "outputs"
        self.work.mkdir()
        self.glb = self.work / "Wooden Bench.glb"
        self.glb.write_bytes(make_glb())
        self.icon = self.work / "Wooden Bench.png"
        self.icon.write_bytes(PNG)
        self.records = [{"part": 0, "files": [self.glb.name], "icon": self.icon.name}]

    def args(self, *extra):
        parser = argparse.ArgumentParser()
        ue.add_arguments(parser)
        return parser.parse_args(["--unity-project", str(self.project), *extra])

    def recognize_project(self):
        for rel in ue.RUNTIME_FILES:
            path = self.project / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("// Fixture runtime type", encoding="utf-8")
        (self.project / ue.DEFAULT_SCENE).write_text("fixture scene", encoding="utf-8")
        path = self.project / ue.LEGACY_TARGET
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(LEGACY)
        (self.project / ue.GENERATOR_TARGET).write_bytes(GENERATOR)

    def test_copy_reuses_exact_icon_and_preserves_guid_files(self):
        model_target = self.project / ue.MODEL_FOLDER / self.glb.name
        icon_target = self.project / ue.ICON_FOLDER / self.icon.name
        for target in (model_target, icon_target):
            target.parent.mkdir(parents=True, exist_ok=True)
            Path(str(target) + ".meta").write_bytes(b"guid: retain-me")
        status = ue.export_assets(self.work, self.records, self.args("--unity-copy"))
        self.assertEqual(model_target.read_bytes(), self.glb.read_bytes())
        self.assertEqual(icon_target.read_bytes(), PNG)
        for target in (model_target, icon_target):
            self.assertEqual(Path(str(target) + ".meta").read_bytes(), b"guid: retain-me")
        sidecar = json.loads(model_target.with_suffix(".prop.json").read_text(encoding="utf-8"))
        self.assertEqual(sidecar["itemId"], "wooden_bench")
        self.assertEqual(sidecar["iconAssetPath"], ue.ICON_FOLDER + "/" + self.icon.name)
        self.assertFalse(model_target.with_suffix(".prop.pending").exists())
        self.assertEqual(status["registration"], "needs_project_integration")

    def test_custom_model_directory_derives_same_project_icon_directory(self):
        parser = argparse.ArgumentParser()
        ue.add_arguments(parser)
        args = parser.parse_args(["--unity-copy", "--unity-dir", str(self.project / "Assets/CustomProps")])
        config = ue.preflight(args)
        self.assertEqual(config["project"], str(self.project.resolve()))
        self.assertEqual(config["icons"], str((self.project / ue.ICON_FOLDER).resolve()))

    def test_invalid_project_and_cross_project_icon_rejected_before_writes(self):
        before = list(self.project.rglob("*"))
        with self.assertRaises(ue.ExportError):
            ue.preflight(self.args("--unity-copy", "--unity-icon-dir", str(self.root / "other/Assets/Icons")))
        self.assertEqual(before, list(self.project.rglob("*")))
        with self.assertRaises(ue.ExportError):
            ue.preflight(self.args("--unity-copy", "--unity-project", str(self.root / "not-unity")))

    def test_manifest_source_traversal_and_missing_icon_do_not_copy_model(self):
        for record in ({"files": ["../bad.glb"], "icon": self.icon.name},
                       {"files": [self.glb.name], "icon": "missing.png"}):
            with self.subTest(record=record), self.assertRaises(ue.ExportError):
                ue.export_assets(self.work, [record], self.args("--unity-copy"))
        self.assertFalse((self.project / ue.MODEL_FOLDER).exists())

    def test_no_icon_is_intentional_and_does_not_require_a_png(self):
        records = [{"files": [self.glb.name]}]
        status = ue.export_assets(self.work, records, self.args("--unity-copy", "--no-icon"))
        self.assertEqual(status["copied_icons_to_unity"], [])
        sidecar = self.project / ue.MODEL_FOLDER / "Wooden Bench.prop.json"
        self.assertIsNone(json.loads(sidecar.read_text())["iconAssetPath"])

    def test_local_metadata_is_retained_without_unity_copy(self):
        result = ue.export_assets(self.work, self.records, self.args())
        self.assertEqual(result["metadata_files"], ["Wooden Bench.prop.json"])
        self.assertEqual(result["copied_to_unity"], [])
        self.assertEqual(json.loads((self.work / result["metadata_files"][0]).read_text())["iconFile"], self.icon.name)

    def test_alternative_formats_allowed_without_unity_copy(self):
        records = [{"files": ["asset.fbx"]}]
        self.assertEqual(ue.export_assets(self.work, records, self.args())["metadata_files"], [])
        with self.assertRaises(ue.ExportError):
            ue.export_assets(self.work, records, self.args("--unity-copy"))

    def test_preserve_designer_metadata_then_explicitly_update(self):
        ue.export_assets(self.work, self.records, self.args("--unity-copy", "--price", "12"))
        sidecar = self.project / ue.MODEL_FOLDER / "Wooden Bench.prop.json"
        data = json.loads(sidecar.read_text())
        data.update(displayName="手动设计的长凳", price=99, footprint=[2, 3], customDesignerField="keep")
        sidecar.write_text(json.dumps(data), encoding="utf-8")
        ue.export_assets(self.work, self.records, self.args("--unity-copy", "--price", "25"))
        preserved = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(preserved["price"], 99)
        self.assertEqual(preserved["displayName"], data["displayName"])
        self.assertEqual(preserved["customDesignerField"], "keep")
        ue.export_assets(self.work, self.records, self.args("--unity-copy", "--price", "25", "--unity-update-metadata"))
        updated = json.loads(sidecar.read_text(encoding="utf-8"))
        self.assertEqual(updated["price"], 25)
        self.assertEqual(updated["footprint"], [2, 3])
        self.assertTrue(updated["updateMetadata"])
        self.assertEqual(updated["updateFields"], ["price"])

    def test_failed_copy_keeps_pending_and_retry_commits_sidecar_last(self):
        original_write = ue._atomic_write
        def failing_write(path, content):
            if str(path).endswith(".glb"):
                raise PermissionError("simulated Unity file lock")
            return original_write(path, content)
        with mock.patch.object(ue, "_atomic_write", side_effect=failing_write), self.assertRaises(PermissionError):
            ue.export_assets(self.work, self.records, self.args("--unity-copy"))
        target = self.project / ue.MODEL_FOLDER / self.glb.name
        self.assertTrue(target.with_suffix(".prop.pending").exists())
        self.assertFalse(target.with_suffix(".prop.json").exists())
        writes = []
        def tracking_write(path, content):
            if self.project in Path(path).parents:
                writes.append(Path(path))
            return original_write(path, content)
        with mock.patch.object(ue, "_atomic_write", side_effect=tracking_write):
            ue.export_assets(self.work, self.records, self.args("--unity-copy"))
        self.assertEqual(writes[-1], target.with_suffix(".prop.json"))
        self.assertLess(writes.index(self.project / ue.ICON_FOLDER / self.icon.name), writes.index(target))
        self.assertFalse(target.with_suffix(".prop.pending").exists())

    def test_install_guard_backup_and_idempotence(self):
        self.recognize_project()
        legacy = self.project / ue.LEGACY_TARGET
        Path(str(legacy) + ".meta").write_bytes(b"legacy-guid")
        result = ue.export_assets(self.work, self.records, self.args("--unity-copy"))
        self.assertEqual(result["importer"]["status"], "installed")
        self.assertEqual(result["registration"], "pending_unity_editor")
        patched = legacy.read_bytes()
        self.assertEqual(patched.count(ue.GUARD_TAG.encode()), 1)
        self.assertEqual(Path(result["importer"]["backups"][0]).read_bytes(), LEGACY)
        self.assertEqual(Path(str(legacy) + ".meta").read_bytes(), b"legacy-guid")
        self.assertIn(b"ImagePartsTo3D.SourceCutout", (self.project / ue.GENERATOR_TARGET).read_bytes())
        result2 = ue.export_assets(self.work, self.records, self.args("--unity-copy"))
        self.assertEqual(result2["importer"]["status"], "already_current")
        self.assertEqual(legacy.read_bytes(), patched)

    def test_differing_bridge_requires_explicit_update_and_backup(self):
        self.recognize_project()
        bridge = self.project / ue.BRIDGE_TARGET
        bridge.write_bytes(b"// custom bridge")
        with self.assertRaises(ue.ExportError):
            ue.preflight(self.args("--unity-copy"))
        self.assertEqual(bridge.read_bytes(), b"// custom bridge")
        result = ue.export_assets(self.work, self.records, self.args("--unity-copy", "--unity-update-importer"))
        self.assertTrue(any(Path(p).read_bytes() == b"// custom bridge" for p in result["importer"]["backups"]))

    def test_changed_legacy_loop_fails_preflight(self):
        self.recognize_project()
        (self.project / ue.LEGACY_TARGET).write_bytes(LEGACY.replace(b"var glbPath in glbs", b"var modelPath in glbs"))
        with self.assertRaises(ue.ExportError):
            ue.preflight(self.args("--unity-copy"))
        self.assertFalse((self.project / ue.BRIDGE_TARGET).exists())

    def test_open_editor_installs_scripts_then_waits_for_compiled_readiness(self):
        self.recognize_project()
        lock = self.project / "Temp/UnityLockfile"
        lock.parent.mkdir()
        lock.write_bytes(b"Unity running")
        args = self.args("--unity-copy")
        for attempt in range(2):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(ue.ExportError, "UNITY_IMPORTER_PENDING"):
                ue.preflight(args)
        self.assertTrue((self.project / ue.BRIDGE_TARGET).exists())
        self.assertFalse((self.project / ue.MODEL_FOLDER).exists())
        ready = {key: hashlib.sha256((self.project / rel).read_bytes()).hexdigest()
                 for key, rel in (("bridgeHash", ue.BRIDGE_TARGET), ("legacyHash", ue.LEGACY_TARGET),
                                  ("generatorHash", ue.GENERATOR_TARGET))}
        ready_path = self.project / "Library/ImagePartsTo3D/importer-ready.json"
        ready_path.write_text(json.dumps(ready), encoding="utf-8")
        result = ue.export_assets(self.work, self.records, args)
        self.assertEqual(result["registration"], "pending_unity_editor")
        self.assertTrue((self.project / ue.MODEL_FOLDER / self.glb.name).exists())

    def test_duplicate_id_in_another_project_folder_is_not_overwritten(self):
        ue.export_assets(self.work, self.records, self.args("--unity-copy"))
        with self.assertRaisesRegex(ue.ExportError, "already managed"):
            ue.export_assets(self.work, self.records, self.args("--unity-copy", "--unity-dir", str(self.project / "Assets/SecondFolder")))
        self.assertFalse((self.project / "Assets/SecondFolder").exists())

    def test_reject_invalid_glb_and_invalid_values(self):
        self.glb.write_bytes(b"not a GLB")
        with self.assertRaises(ue.ExportError):
            ue.export_assets(self.work, self.records, self.args("--unity-copy"))
        for options in (("--price", "-1"), ("--world-scale", "nan"), ("--world-scale", "0.05"), ("--footprint", "0", "1")):
            with self.subTest(options=options), self.assertRaises(ue.ExportError):
                ue.preflight(self.args(*options))

    def test_collision_and_explicit_id_mismatch_rejected(self):
        other = self.work / "Wooden-Bench.glb"
        other.write_bytes(self.glb.read_bytes())
        records = self.records + [{"files": [other.name], "icon": self.icon.name}]
        with self.assertRaises(ue.ExportError):
            ue.export_assets(self.work, records, self.args("--unity-copy"))
        with self.assertRaises(ue.ExportError):
            ue.export_assets(self.work, self.records, self.args("--item-id", "another_prop"))

    def test_cli_retry_uses_manifest_directory_after_it_moves(self):
        manifest = self.work / "manifest.json"
        manifest.write_text(json.dumps({"work_dir": "C:/untrusted-old-location", "parts": self.records}), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            result = ue.main(["--manifest", str(manifest), "--unity-copy", "--unity-project", str(self.project)])
        self.assertEqual(result, 0)
        self.assertTrue((self.project / ue.MODEL_FOLDER / self.glb.name).exists())

    def test_manifest_retry_restores_options_updates_status_and_allows_overrides(self):
        manifest = self.work / "manifest.json"
        data = {"parts": self.records, "status": "export_failed", "unity": {"error": "old failure"},
                "failed": [{"part": 1, "error": "generation failed"}],
                "unity_options": {"unity_copy": True, "unity_project": str(self.project), "price": 17,
                                  "category": "Building", "world_scale": 2.5, "footprint": [2, 1]}}
        manifest.write_text(json.dumps(data), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            code = ue.main(["--manifest", str(manifest), "--price", "23", "--unity-update-metadata"])
        self.assertEqual(code, 0)
        recovered = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(recovered["status"], "partial_failure")
        self.assertNotIn("error", recovered["unity"])
        metadata = json.loads((self.project / ue.MODEL_FOLDER / "Wooden Bench.prop.json").read_text(encoding="utf-8"))
        self.assertEqual(metadata["price"], 23)
        self.assertEqual(metadata["footprint"], [2, 1])
        self.assertEqual(metadata["updateFields"], ["price"])
        with contextlib.redirect_stdout(io.StringIO()):
            code = ue.main(["--manifest", str(manifest), "--no-unity-copy"])
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(manifest.read_text(encoding="utf-8"))["unity_options"]["unity_copy"])

    def test_sanitization_and_prop_category(self):
        self.assertEqual(ue.sanitize_id("大树-01.test"), "大树_01_test")
        self.assertEqual(ue._default_category("treehouse"), "Building")
        self.assertEqual(ue._default_category("palm_tree"), "Plant")
        self.assertEqual(ue._default_category("rock"), "Decoration")


if __name__ == "__main__":
    unittest.main()
