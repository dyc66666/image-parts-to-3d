#!/usr/bin/env python3
"""Prepare/retry Unity prop exports without calling a generation service.

The .prop.json is the commit marker. A .prop.pending marker blocks both
importers while existing files are being replaced. Unity .meta files are never
copied, deleted or replaced, so existing asset GUIDs survive updates.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import time
import unicodedata


DEFAULT_PROJECT = Path(r"F:\unity\My project")
MODEL_FOLDER = "Assets/AnimalHome/Imported3D/SceneProps"
ICON_FOLDER = "Assets/AnimalHome/Icons/SourceCutouts"
DEFAULT_SCENE = "Assets/DropBY.unity"
BRIDGE_TARGET = "Assets/DropBy/Scripts/Editor/ImagePartsTo3DImporter.cs"
LEGACY_TARGET = "Assets/DropBy/Scripts/Editor/DropByScenePropImporter.cs"
GENERATOR_TARGET = "Assets/DropBy/Scripts/Editor/DropByModelIconGenerator.cs"
BRIDGE_SOURCE = Path(__file__).resolve().parents[1] / "assets/unity/ImagePartsTo3DImporter.cs"
RUNTIME_FILES = (
    "Assets/AnimalHome/Scripts/PlaceableItemData.cs",
    "Assets/AnimalHome/Scripts/PlacementController.cs",
    "Assets/DropBy/Scripts/Runtime/IslandBuildController.cs",
)
GUARD_TAG = "// image-parts-to-3d: managed props use the metadata importer."
ICON_GUARD_TAG = "// image-parts-to-3d: preserve labeled source cutout icons."
DESIGN_FIELDS = ("displayName", "category", "price", "worldScale", "footprint")
BOOL_OPTIONS = ("unity_copy", "no_icon", "unity_no_importer", "unity_update_importer", "unity_update_metadata")
STRING_OPTIONS = ("unity_project", "unity_dir", "unity_icon_dir", "unity_scene", "item_id", "display_name", "category")
SAVED_OPTIONS = BOOL_OPTIONS + STRING_OPTIONS + ("price", "world_scale", "footprint", "unity_update_fields")


class ExportError(ValueError):
    pass


def add_arguments(parser):
    group = parser.add_argument_group("Unity prop export")
    group.add_argument("--unity-copy", action="store_true", help="Copy GLB, original cutout and import metadata into Unity")
    group.add_argument("--no-unity-copy", dest="unity_copy", action="store_false", default=argparse.SUPPRESS, help="Override a saved manifest's Unity copy setting")
    group.add_argument("--unity-project", help="Unity project root (default F:\\unity\\My project)")
    group.add_argument("--unity-dir", help="Model destination inside project Assets; selects its own project if provided")
    group.add_argument("--unity-icon-dir", help="Icon destination inside the SAME project (default AnimalHome/Icons/SourceCutouts)")
    group.add_argument("--no-icon", action="store_true", help="Do not export the source cutout icon")
    group.add_argument("--with-icon", dest="no_icon", action="store_false", default=argparse.SUPPRESS, help="Override a saved manifest's --no-icon setting")
    group.add_argument("--unity-scene", help="Assets-relative toolbar scene (default Assets/DropBY.unity)")
    group.add_argument("--unity-no-importer", action="store_true", help="Copy files only, without installing the AnimalHome editor bridge")
    group.add_argument("--unity-update-importer", action="store_true", help="Back up and replace a differing installed bridge")
    group.add_argument("--unity-update-metadata", action="store_true", help="Apply explicitly supplied designer fields to existing items")
    group.add_argument("--item-id", help="Single prop ID; must equal the sanitized model filename stem")
    group.add_argument("--display-name", help="Toolbar display name (one prop only)")
    group.add_argument("--category", choices=["Plant", "Building", "Decoration", "Terrain", "Animal"], help="New prop category; Terrain/Animal may be hidden by this game's toolbar")
    group.add_argument("--price", type=int, help="Nonnegative in-game price")
    group.add_argument("--world-scale", type=float, help="Positive placement scale")
    group.add_argument("--footprint", type=int, nargs=2, metavar=("X", "Z"), help="Positive grid footprint")


def _arg(args, name, default=None):
    return getattr(args, name, default)


def sanitize_id(stem):
    """Match the existing C# char.IsLetterOrDigit/SanitizeId implementation."""
    chars = []
    for char in stem.lower():
        if char in " .-":
            chars.append("_")
        elif char == "_" or (ord(char) <= 0xFFFF and
                             (unicodedata.category(char).startswith("L") or unicodedata.category(char) == "Nd")):
            chars.append(char)
    result = "".join(chars).strip("_")
    if not result:
        raise ExportError("Model filename has no usable item ID; choose a meaningful --name")
    return result


def _inside(path, root, label):
    path, root = Path(path).resolve(), Path(root).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise ExportError(f"{label} must stay inside {root}: {path}") from None
    return path


def _basename(value, label):
    if not isinstance(value, str) or not value or any(c in value for c in '/\\:\x00'):
        raise ExportError(f"{label} must be a filename, not a path: {value!r}")
    if value in (".", "..") or value.rstrip(" .") != value or any(c in value for c in '<>\"|?*'):
        raise ExportError(f"Invalid {label}: {value!r}")
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])", value.split(".")[0]):
        raise ExportError(f"Reserved Windows filename: {value}")
    return value


def _source(root, value, label):
    path = _inside(Path(root) / _basename(value, label), root, label)
    if not path.is_file():
        raise ExportError(f"Missing {label}: {path}")
    return path


def _validate_options(args):
    for key in BOOL_OPTIONS:
        if type(_arg(args, key, False)) is not bool:
            raise ExportError(f"Invalid boolean export option: {key}")
    for key in STRING_OPTIONS:
        if _arg(args, key) is not None and not isinstance(_arg(args, key), str):
            raise ExportError(f"Invalid text export option: {key}")
    if _arg(args, "category") not in (None, "Plant", "Building", "Decoration", "Terrain", "Animal"):
        raise ExportError("Unknown item category in export options")
    selected = _arg(args, "unity_update_fields")
    if selected is not None and (not isinstance(selected, list) or any(field not in DESIGN_FIELDS for field in selected)):
        raise ExportError("Invalid explicit metadata update field list")
    price, scale, footprint = (_arg(args, k) for k in ("price", "world_scale", "footprint"))
    if price is not None and (type(price) is not int or price < 0):
        raise ExportError("--price must be nonnegative")
    if scale is not None and (type(scale) not in (int, float) or not math.isfinite(scale) or scale < 0.1):
        raise ExportError("--world-scale must be finite and at least 0.1 (the game's minimum)")
    if footprint is not None and (not isinstance(footprint, (list, tuple)) or len(footprint) != 2 or any(type(v) is not int or v < 1 for v in footprint)):
        raise ExportError("--footprint needs two positive integers")
    for field in ("item_id", "display_name"):
        value = _arg(args, field)
        if value is not None and not value.strip():
            raise ExportError(f"--{field.replace('_', '-')} cannot be empty")


def _legacy_patch(raw):
    """Patch ONLY ImportNewProps, leaving explicit legacy repair tools intact."""
    text = raw.decode("utf-8-sig")
    method = re.search(r"public\s+static\s+int\s+ImportNewProps\s*\(\s*\)", text)
    if not method:
        raise ExportError("Legacy importer has changed: cannot safely locate ImportNewProps()")
    next_method = re.search(r"\n\s+(?:public|private|internal)\s+static\s+", text[method.end():])
    end = method.end() + next_method.start() if next_method else len(text)
    body = text[method.end():end]
    if GUARD_TAG in body:
        if '.prop.pending' not in body or '.prop.json' not in body:
            raise ExportError("Installed managed-prop guard is incomplete; inspect the legacy importer")
        return raw
    anchor = re.search(r"foreach\s*\(var glbPath in glbs\)\s*\{\s*(?=string id = SanitizeId\(Path.GetFileNameWithoutExtension\(glbPath\)\);)", body)
    if not anchor:
        raise ExportError("Legacy ImportNewProps loop has changed; refusing to modify it automatically")
    newline = "\r\n" if "\r\n" in text else "\n"
    guard = (GUARD_TAG + newline +
             '                if (File.Exists(Path.ChangeExtension(glbPath, ".prop.json")) ||' + newline +
             '                    File.Exists(Path.ChangeExtension(glbPath, ".prop.pending"))) continue;' + newline +
             '                ')
    pos = method.end() + anchor.end()
    patched = (text[:pos] + guard + text[pos:]).encode("utf-8")
    return (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + patched


def _icon_generator_patch(raw):
    text = raw.decode("utf-8-sig")
    method = re.search(r"private\s+static\s+bool\s+IsSourceCutoutIcon\(PlaceableItemData item\)", text)
    if not method:
        raise ExportError("Legacy icon generator has changed: cannot locate IsSourceCutoutIcon")
    next_method = re.search(r"\n\s+private\s+static\s+", text[method.end():])
    end = method.end() + next_method.start() if next_method else len(text)
    body = text[method.end():end]
    if ICON_GUARD_TAG in body:
        if '"ImagePartsTo3D.SourceCutout"' not in body:
            raise ExportError("Installed source-icon guard is incomplete")
        return raw
    anchor = re.search(r"if \(item\?\.icon == null\) return false;", body)
    if not anchor:
        raise ExportError("Legacy source-icon check has changed; refusing to modify it automatically")
    newline = "\r\n" if "\r\n" in text else "\n"
    addition = (newline + "            " + ICON_GUARD_TAG + newline +
                '            if (System.Array.IndexOf(AssetDatabase.GetLabels(AssetDatabase.LoadMainAssetAtPath(' + newline +
                '                AssetDatabase.GetAssetPath(item.icon))), "ImagePartsTo3D.SourceCutout") >= 0) return true;')
    pos = method.end() + anchor.end()
    patched = (text[:pos] + addition + text[pos:]).encode("utf-8")
    return (b"\xef\xbb\xbf" if raw.startswith(b"\xef\xbb\xbf") else b"") + patched


def _source_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""


def _editor_ready(config):
    project = Path(config["project"])
    ready_path = _inside(project / "Library/ImagePartsTo3D/importer-ready.json", project, "Importer readiness")
    try:
        ready = json.loads(ready_path.read_text(encoding="utf-8-sig"))
        return (ready.get("bridgeHash") == _source_hash(project / BRIDGE_TARGET) == _source_hash(BRIDGE_SOURCE)
                and ready.get("legacyHash", "") == _source_hash(project / LEGACY_TARGET)
                and ready.get("generatorHash", "") == _source_hash(project / GENERATOR_TARGET))
    except (OSError, ValueError, AttributeError):
        return False


def preflight(args):
    """Validate before spending credits. In an open editor, install only the
    needed scripts and stop until a successful compile acknowledges readiness.
    No GLB/icon is copied here; a closed editor needs no handshake.
    """
    _validate_options(args)
    if not _arg(args, "unity_copy", False):
        return {"enabled": False}
    custom_models = _arg(args, "unity_dir")
    project_arg = _arg(args, "unity_project")
    models = Path(custom_models).expanduser().resolve() if custom_models else None
    if project_arg:
        project = Path(project_arg).expanduser().resolve()
    elif models:
        assets_parent = next((p for p in (models, *models.parents) if p.name.lower() == "assets"), None)
        if assets_parent is None:
            raise ExportError("--unity-dir must be inside a Unity project's Assets folder")
        project = assets_parent.parent
    else:
        project = DEFAULT_PROJECT.expanduser().resolve()
    assets = project / "Assets"
    if not assets.is_dir() or not (project / "ProjectSettings").is_dir():
        raise ExportError(f"Not a Unity project (Assets and ProjectSettings are required): {project}")
    models = _inside(models or project / MODEL_FOLDER, assets, "Unity model directory")
    icons = _inside(Path(_arg(args, "unity_icon_dir")).expanduser() if _arg(args, "unity_icon_dir")
                    else project / ICON_FOLDER, assets, "Unity icon directory")
    if models == assets.resolve() or icons == assets.resolve():
        raise ExportError("Use a subfolder of Assets for generated props/icons")
    for folder in (models, icons):
        if folder.exists() and not folder.is_dir():
            raise ExportError(f"Destination is not a directory: {folder}")
    scene = (_arg(args, "unity_scene") or DEFAULT_SCENE).replace("\\", "/")
    if not scene.startswith("Assets/") or not scene.lower().endswith(".unity"):
        raise ExportError("--unity-scene must be an Assets-relative .unity path")
    _inside(project / scene, assets, "Toolbar scene")
    recognized = all((project / rel).is_file() for rel in RUNTIME_FILES)
    install = recognized and not _arg(args, "unity_no_importer", False)
    if install:
        if not (project / scene).is_file():
            raise ExportError(f"Toolbar scene is missing: {scene}; use --unity-scene for its actual path")
        if not BRIDGE_SOURCE.is_file():
            raise ExportError(f"Packaged Unity importer is missing: {BRIDGE_SOURCE}")
        bridge = _inside(project / BRIDGE_TARGET, assets, "Unity importer")
        if bridge.exists() and bridge.read_bytes() != BRIDGE_SOURCE.read_bytes() and not _arg(args, "unity_update_importer", False):
            raise ExportError("Installed ImagePartsTo3DImporter.cs differs; review it, then use --unity-update-importer to back it up and replace it")
        legacy = _inside(project / LEGACY_TARGET, assets, "Legacy importer")
        if legacy.exists():
            _legacy_patch(legacy.read_bytes())
        generator = _inside(project / GENERATOR_TARGET, assets, "Legacy icon generator")
        if generator.exists():
            _icon_generator_patch(generator.read_bytes())
    config = {"enabled": True, "project": str(project), "models": str(models), "icons": str(icons),
              "scene": scene, "recognized_project": recognized, "install_importer": install}
    if install and (project / "Temp/UnityLockfile").exists():
        # Installing source while Unity runs does not make its previously
        # compiled legacy watcher safe yet. Readiness comes from the newly
        # compiled bridge's InitializeOnLoad constructor, never a file timer.
        _install_importer(config)
        if not _editor_ready(config):
            raise ExportError("UNITY_IMPORTER_PENDING: editor scripts are installed, but Unity has not acknowledged their successful compilation. Wait for Unity to compile (resolve Console errors), or close Unity, then retry this same command. No generation or GLB copy is required for this setup step.")
    return config


def _atomic_write(path, content):
    path = Path(path)
    if path.is_file() and path.read_bytes() == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(4):
            try:
                os.replace(temporary, path)
                return True
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.1 * (attempt + 1))
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _json_bytes(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _read_metadata(path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict) or data.get("schemaVersion", 1) != 1:
            raise ValueError("unsupported metadata schema")
        return data
    except (ValueError, OSError) as exc:
        raise ExportError(f"Cannot preserve existing metadata {path}: {exc}") from exc


def _default_category(item_id):
    if any(word in item_id for word in ("treehouse", "house", "hut", "cabin", "tower", "bridge", "stall", "market", "shop", "windmill", "platform", "deck", "dock", "barn", "shed", "屋", "房", "桥", "塔")):
        return "Building"
    if any(word in item_id for word in ("tree", "palm", "bush", "plant", "grass", "flower", "fern", "leaf", "mushroom", "vine", "shrub", "树", "草", "花", "蘑菇")):
        return "Plant"
    # In the current game Terrain opens sculpt tools, hiding placeable rocks.
    return "Decoration"


def _metadata(item_id, model_name, icon_name, existing, args, scene, icon_asset=None):
    if existing.get("itemId", item_id) != item_id:
        raise ExportError(f"Existing metadata itemId differs for {model_name}; refusing to overwrite another item")
    data = dict(existing)
    fields = {"displayName": _arg(args, "display_name"), "category": _arg(args, "category"),
              "price": _arg(args, "price"), "worldScale": _arg(args, "world_scale"),
              "footprint": _arg(args, "footprint")}
    for key, value in fields.items():
        if value is not None and (key not in existing or _arg(args, "unity_update_metadata", False)):
            data[key] = value
    data.setdefault("category", _default_category(item_id))
    data.update(schemaVersion=1, itemId=item_id, modelFile=model_name, iconFile=icon_name,
                iconAssetPath=icon_asset, updateMetadata=bool(_arg(args, "unity_update_metadata", False)),
                sceneAssetPath=scene)
    selected = _arg(args, "unity_update_fields")
    data["updateFields"] = (selected if selected is not None else [key for key, value in fields.items() if value is not None]) if data["updateMetadata"] else []
    return data


def _validate_glb(path):
    with path.open("rb") as handle:
        header = handle.read(12)
    if len(header) != 12:
        raise ExportError(f"Incomplete GLB: {path}")
    magic, version, length = struct.unpack("<4sII", header)
    if magic != b"glTF" or version != 2 or length != path.stat().st_size or length < 20:
        raise ExportError(f"Invalid/truncated GLB 2.0: {path}")


def _validate_png(path):
    with path.open("rb") as handle:
        header = handle.read(33)
    if len(header) < 33 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ExportError(f"The source cutout must be a PNG: {path}")
    width, height = struct.unpack(">II", header[16:24])
    if width < 1 or height < 1:
        raise ExportError(f"Invalid PNG dimensions: {path}")


def _backup(project, target):
    raw = target.read_bytes()
    backup = project / "Library/ImagePartsTo3D/Backups" / (target.name + "." + hashlib.sha256(raw).hexdigest()[:16] + ".bak")
    _inside(backup, project, "Importer backup")
    _atomic_write(backup, raw)
    return str(backup)


def _install_importer(config):
    if not config["install_importer"]:
        return {"status": "not_installed", "reason": "disabled_or_unrecognized_project"}
    project = Path(config["project"])
    backups = []
    any_changed = False
    ready_path = _inside(project / "Library/ImagePartsTo3D/importer-ready.json", project, "Importer readiness")
    legacy = project / LEGACY_TARGET
    if legacy.exists():
        raw = legacy.read_bytes()
        patched = _legacy_patch(raw)
        if patched != raw:
            if ready_path.exists():
                ready_path.unlink()
            backups.append(_backup(project, legacy))
            _atomic_write(legacy, patched)
            any_changed = True
    generator = project / GENERATOR_TARGET
    if generator.exists():
        raw = generator.read_bytes()
        patched = _icon_generator_patch(raw)
        if patched != raw:
            if ready_path.exists():
                ready_path.unlink()
            backups.append(_backup(project, generator))
            _atomic_write(generator, patched)
            any_changed = True
    bridge = project / BRIDGE_TARGET
    content = BRIDGE_SOURCE.read_bytes()
    if bridge.exists() and bridge.read_bytes() != content:
        backups.append(_backup(project, bridge))
    if not bridge.exists() or bridge.read_bytes() != content:
        if ready_path.exists():
            ready_path.unlink()
    changed = _atomic_write(bridge, content)
    return {"status": "installed" if changed or any_changed else "already_current", "path": str(bridge), "backups": backups}


def export_assets(work_dir, records, args):
    """Create retained sidecars; optionally copy committed assets into Unity.

    records are pipeline manifest parts: {part, files: [basenames], icon: PNG
    basename}. Paths from a manifest cannot escape work_dir. All inputs are
    validated before any prop-file mutation. Readiness preflight may install
    editor scripts. Raises ExportError/OSError on
    failure so a caller can record it and retry using this module's CLI.
    """
    config = preflight(args)
    work = Path(work_dir).resolve()
    prepared, ids = [], set()
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("files", []), list):
            raise ExportError("Each manifest part must contain a files list")
        for filename in record.get("files", []):
            _basename(filename, "model filename")
            if not filename.lower().endswith(".glb"):
                continue
            model = _source(work, filename, "model")
            _validate_glb(model)
            item_id = sanitize_id(model.stem)
            if item_id in ids:
                raise ExportError(f"Multiple GLBs map to the same itemId: {item_id}")
            ids.add(item_id)
            icon = None
            if not _arg(args, "no_icon", False):
                override = _arg(args, "source_icon_override")
                icon = Path(override).expanduser().resolve() if override else _source(work, record.get("icon"), "source cutout")
                if not icon.is_file():
                    raise ExportError(f"Missing source cutout: {icon}")
                _validate_png(icon)
            prepared.append((model, icon, item_id))
    if not prepared and config["enabled"]:
        raise ExportError("Unity export needs at least one generated GLB; no GLB was found")
    if len(prepared) != 1 and (_arg(args, "item_id") or _arg(args, "display_name")):
        raise ExportError("--item-id and --display-name require a single GLB; export parts separately to name them")
    if _arg(args, "item_id") and prepared[0][2] != _arg(args, "item_id"):
        raise ExportError("--item-id must equal the sanitized model filename; rename the source GLB to change its identity")
    scene = config.get("scene", _arg(args, "unity_scene") or DEFAULT_SCENE)
    planned = []
    for model, icon, item_id in prepared:
        local_sidecar = _inside(model.with_suffix(".prop.json"), work, "Local metadata")
        local_data = _metadata(item_id, model.name, icon.name if icon else None,
                               _read_metadata(local_sidecar), args, scene)
        target_model, target_icon, target_sidecar, target_data = (None,) * 4
        if config["enabled"]:
            project = Path(config["project"])
            target_model = _inside(Path(config["models"]) / model.name, project / "Assets", "Target GLB")
            target_icon = _inside(Path(config["icons"]) / (model.stem + ".png"), project / "Assets", "Target icon") if icon else None
            target_sidecar = _inside(target_model.with_suffix(".prop.json"), project / "Assets", "Target metadata")
            icon_asset = target_icon.relative_to(project).as_posix() if icon else None
            # Preserve target designer values over local values on an update.
            base = dict(local_data)
            base.update(_read_metadata(target_sidecar))
            target_data = _metadata(item_id, model.name, icon.name if icon else None, base, args, scene, icon_asset)
        planned.append((model, icon, local_sidecar, local_data, target_model, target_icon, target_sidecar, target_data))
    if config["enabled"]:
        expected_paths = {entry[7]["itemId"]: entry[6] for entry in planned}
        for existing_path in (Path(config["project"]) / "Assets").rglob("*.prop.json"):
            existing_path = _inside(existing_path, Path(config["project"]) / "Assets", "Existing prop metadata")
            try:
                existing_id = json.loads(existing_path.read_text(encoding="utf-8-sig")).get("itemId")
            except (ValueError, OSError, AttributeError):
                continue  # An unrelated invalid sidecar is handled by Unity.
            if existing_id in expected_paths and existing_path != expected_paths[existing_id]:
                raise ExportError(f"itemId {existing_id} is already managed by a different model: {existing_path}")
    result = {"metadata_files": [], "copied_to_unity": [], "copied_icons_to_unity": [],
              "copied_metadata_to_unity": [], "registration": "not_requested", "items": [], "warnings": []}
    for _, _, local_sidecar, local_data, _, _, target_sidecar, _ in planned:
        # A standalone --model may already be in the destination folder. Do
        # not publish its sidecar before the pending marker/commit sequence.
        if local_sidecar != target_sidecar:
            _atomic_write(local_sidecar, _json_bytes(local_data))
        result["metadata_files"].append(local_sidecar.name)
    if not config["enabled"]:
        return result
    result["importer"] = _install_importer(config)
    result["registration"] = "pending_unity_editor" if config["install_importer"] else "needs_project_integration"
    project = Path(config["project"])
    for model, icon, _, _, target_model, target_icon, target_sidecar, target_data in planned:
        marker = _inside(target_model.with_suffix(".prop.pending"), project / "Assets", "Pending marker")
        _atomic_write(marker, _json_bytes({"itemId": target_data["itemId"], "modelFile": model.name}))
        # Keep the pending marker on failure: an old sidecar must never publish
        # a partially updated model/icon pair. A successful retry removes it.
        if icon:
            _atomic_write(target_icon, icon.read_bytes())
            result["copied_icons_to_unity"].append(str(target_icon))
        _atomic_write(target_model, model.read_bytes())
        result["copied_to_unity"].append(str(target_model))
        _atomic_write(target_sidecar, _json_bytes(target_data))
        result["copied_metadata_to_unity"].append(str(target_sidecar))
        marker.unlink()
        result["items"].append({"itemId": target_data["itemId"], "model": str(target_model),
                                "metadata": str(target_sidecar),
                                "status_file": str(project / "Library/ImagePartsTo3D" / (target_data["itemId"] + ".status.json"))})
        if target_data.get("category") in ("Terrain", "Animal"):
            result["warnings"].append(f"{target_data['itemId']}: category {target_data['category']} may not be shown by the current prop toolbar")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", help="Existing pipeline manifest.json; no Hunyuan request is made")
    source.add_argument("--model", help="Existing GLB to export")
    parser.add_argument("--icon", help="Earlier background-removed PNG, required with --model unless --no-icon")
    add_arguments(parser)
    args = parser.parse_args(argv)
    manifest_path, data = None, None
    try:
        if args.manifest:
            if args.icon:
                raise ExportError("Use each manifest part's icon field; --icon is only for --model")
            manifest_path = Path(args.manifest).expanduser().resolve()
            data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ExportError("Manifest must be a JSON object")
            stored_options = data.get("unity_options", {})
            if not isinstance(stored_options, dict):
                raise ExportError("Manifest unity_options must be an object")
            parser.set_defaults(**{key: value for key, value in stored_options.items() if key in SAVED_OPTIONS})
            args = parser.parse_args(argv)  # Explicit CLI flags override saved values.
            field_flags = {"--display-name": "displayName", "--category": "category", "--price": "price",
                           "--world-scale": "worldScale", "--footprint": "footprint"}
            explicit_fields = [field for flag, field in field_flags.items()
                               if any(token == flag or token.startswith(flag + "=") for token in (argv if argv is not None else sys.argv[1:]))]
            if explicit_fields:
                args.unity_update_fields = explicit_fields
            records = data.get("parts")
            if not isinstance(records, list):
                raise ExportError("Manifest must contain a parts list")
            # The manifest's own directory is authoritative after moving a
            # package; never trust its old/possibly injected work_dir field.
            work = manifest_path.parent
        else:
            model = Path(args.model).expanduser().resolve()
            work = model.parent
            if not args.icon and not args.no_icon:
                raise ExportError("--model requires the original --icon PNG, or intentional --no-icon")
            args.source_icon_override = args.icon
            records = [{"part": 0, "files": [model.name], "icon": Path(args.icon).name if args.icon else None}]
        result = export_assets(work, records, args)
        if manifest_path is not None:
            data["unity"] = result
            data["status"] = "partial_failure" if data.get("failed") else "complete"
            data["unity_options"] = {key: _arg(args, key) for key in SAVED_OPTIONS}
            if data["unity_options"]["unity_update_fields"] is None:
                data["unity_options"]["unity_update_fields"] = [field for field, option in
                    (("displayName", "display_name"), ("category", "category"), ("price", "price"),
                     ("worldScale", "world_scale"), ("footprint", "footprint")) if _arg(args, option) is not None]
            _atomic_write(manifest_path, _json_bytes(data))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ExportError, OSError, ValueError) as exc:
        if manifest_path is not None and isinstance(data, dict):
            data["unity"] = {"error": str(exc)}
            data["status"] = "export_failed"
            try:
                _atomic_write(manifest_path, _json_bytes(data))
            except OSError:
                pass  # Preserve the original actionable error if the folder is locked.
        print(json.dumps({"error": "UNITY_EXPORT_FAILED", "message": str(exc),
                          "hint": "Original GLB and cutout are retained; fix the issue and retry without regenerating."}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
