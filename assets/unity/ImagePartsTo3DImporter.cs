#if UNITY_EDITOR
// image-parts-to-3d bridge v1.0.0. Install in an Editor folder in the AnimalHome project.
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using AnimalHome;
using DropBy.UI;
using UnityEditor;
using UnityEditor.SceneManagement;
using UnityEngine;
using UnityEngine.SceneManagement;
using Object = UnityEngine.Object;

namespace ImagePartsTo3D.Editor
{
    [Serializable]
    public sealed class PropMetadata
    {
        public int schemaVersion = 1;
        public string itemId, modelFile, iconAssetPath, displayName, category;
        public int price = -1;
        public float worldScale;
        public int[] footprint;
        public bool updateMetadata;
        public string[] updateFields;
        public string sceneAssetPath = "Assets/DropBY.unity";
    }

    [Serializable]
    public sealed class PropImportStatus
    {
        public int schemaVersion = 1;
        public string itemId, status, message, metadataPath, modelPath, iconPath;
        public string prefabPath, itemPath, sceneAssetPath, utc;
        public string modelHash, metadataHash, iconHash;
    }

    [Serializable]
    sealed class ImporterReady
    {
        public string bridgeHash, legacyHash, generatorHash;
    }

    [InitializeOnLoad]
    public static class ImagePartsTo3DImporter
    {
        const string ItemRoot = "Assets/AnimalHome/Data/Items";
        const string PrefabRoot = "Assets/AnimalHome/Prefabs";
        const string StateRoot = "Library/ImagePartsTo3D";
        const string ManagedModel = "ImagePartsTo3D_Model";
        static readonly Dictionary<string, int> Pending = new Dictionary<string, int>(StringComparer.OrdinalIgnoreCase);
        static bool processing;
        static double nextAttempt;

        static ImagePartsTo3DImporter()
        {
            // Allows an external copier to wait for the newly installed bridge/legacy guard
            // to compile before publishing GLBs into an already running editor.
            const string bridgePath = "Assets/DropBy/Scripts/Editor/ImagePartsTo3DImporter.cs";
            const string legacyPath = "Assets/DropBy/Scripts/Editor/DropByScenePropImporter.cs";
            const string generatorPath = "Assets/DropBy/Scripts/Editor/DropByModelIconGenerator.cs";
            if (File.Exists(bridgePath))
            {
                Directory.CreateDirectory(StateRoot);
                File.WriteAllText(StateRoot + "/importer-ready.json", JsonUtility.ToJson(new ImporterReady
                {
                    bridgeHash = FileHash(bridgePath),
                    legacyHash = File.Exists(legacyPath) ? FileHash(legacyPath) : "",
                    generatorHash = File.Exists(generatorPath) ? FileHash(generatorPath) : ""
                }, true), new UTF8Encoding(false));
            }
            EditorApplication.update += Tick;
            EditorApplication.delayCall += Scan;
            EditorSceneManager.sceneOpened += (scene, mode) => Scan();
            EditorSceneManager.sceneSaved += scene => Scan();
            EditorApplication.playModeStateChanged += state =>
            {
                if (state == PlayModeStateChange.EnteredEditMode) Scan();
            };
        }

        [MenuItem("Tools/Drop By/图片转3D/同步模型、原图图标和道具栏")]
        public static void Scan()
        {
            if (processing || !Directory.Exists("Assets")) return;
            foreach (string file in Directory.GetFiles("Assets", "*.prop.json", SearchOption.AllDirectories))
                Enqueue(file);
        }

        internal static void AssetsChanged(string[] imported, string[] deleted, string[] moved)
        {
            if (processing) return;
            var paths = imported.Concat(deleted).Concat(moved).ToArray();
            if (paths.Any(p => p.EndsWith(".prop.json", StringComparison.OrdinalIgnoreCase))) Scan();
            else if (paths.Any(p => p.EndsWith(".glb", StringComparison.OrdinalIgnoreCase) ||
                                   p.EndsWith(".png", StringComparison.OrdinalIgnoreCase) ||
                                   p.EndsWith(".prop.pending", StringComparison.OrdinalIgnoreCase))) Scan();
        }

        static void Enqueue(string path)
        {
            Pending[path.Replace('\\', '/')] = 0;
            nextAttempt = EditorApplication.timeSinceStartup + 0.5;
        }

        static void Tick()
        {
            if (processing || Pending.Count == 0 || EditorApplication.timeSinceStartup < nextAttempt ||
                EditorApplication.isCompiling || EditorApplication.isUpdating ||
                EditorApplication.isPlayingOrWillChangePlaymode) return;
            processing = true;
            try
            {
                foreach (string path in Pending.Keys.ToArray())
                {
                    bool retry = ImportOne(path);
                    if (!retry || ++Pending[path] >= 8) Pending.Remove(path);
                }
            }
            finally { processing = false; nextAttempt = EditorApplication.timeSinceStartup + 2; }
        }

        static bool ImportOne(string metadataPath)
        {
            PropMetadata meta = null;
            bool mayWriteStatus = true;
            PropImportStatus status = new PropImportStatus { metadataPath = metadataPath };
            try
            {
                if (!File.Exists(metadataPath)) return false;
                string pendingPath = metadataPath.Substring(0, metadataPath.Length - ".json".Length) + ".pending";
                if (File.Exists(pendingPath)) return true; // A multi-file copy has not committed yet.
                meta = JsonUtility.FromJson<PropMetadata>(File.ReadAllText(metadataPath, Encoding.UTF8));
                Validate(meta, metadataPath);
                status.itemId = meta.itemId;
                status.modelPath = AssetPath(Path.GetDirectoryName(metadataPath) + "/" + meta.modelFile);
                status.iconPath = meta.iconAssetPath ?? "";
                status.itemPath = ItemRoot + "/" + meta.itemId + ".asset";
                status.prefabPath = PrefabRoot + "/AH_" + meta.itemId + ".prefab";
                status.sceneAssetPath = string.IsNullOrEmpty(meta.sceneAssetPath) ? "Assets/DropBY.unity" : meta.sceneAssetPath;
                var previous = ReadStatus(meta.itemId);
                if (previous != null && !string.Equals(previous.metadataPath, metadataPath, StringComparison.OrdinalIgnoreCase))
                {
                    mayWriteStatus = false; // Never replace the actual owner's state with a conflict report.
                    throw new InvalidOperationException("其他元数据已占用 itemId；为不同道具使用不同名称：" + previous.metadataPath);
                }
                if (previous == null)
                {
                    foreach (string candidate in Directory.GetFiles("Assets", "*.prop.json", SearchOption.AllDirectories))
                    {
                        string otherPath = candidate.Replace('\\', '/');
                        if (string.Equals(otherPath, metadataPath, StringComparison.OrdinalIgnoreCase)) continue;
                        PropMetadata other = null;
                        try { other = JsonUtility.FromJson<PropMetadata>(File.ReadAllText(candidate)); }
                        catch { continue; }
                        if (other != null && other.itemId == meta.itemId)
                        {
                            mayWriteStatus = false;
                            throw new InvalidOperationException("多个 .prop.json 使用相同 itemId；请先为其分配不同名称：" + otherPath);
                        }
                    }
                }
                status.modelHash = DependencyHash(status.modelPath);
                status.metadataHash = FileHash(metadataPath);
                status.iconHash = string.IsNullOrEmpty(status.iconPath) ? "" : DependencyHash(status.iconPath);
                var model = LoadModel(status.modelPath);
                if (model == null) throw new IOException("GLB 尚未导入为 GameObject；检查 glTFast 安装、GLB 文件及 Unity Console。");
                Sprite icon = null;
                if (!string.IsNullOrEmpty(status.iconPath)) icon = LoadIcon(status.iconPath);
                EnsureFolder(ItemRoot);
                EnsureFolder(PrefabRoot);
                var item = AssetDatabase.LoadAssetAtPath<PlaceableItemData>(status.itemPath);
                if (item == null && File.Exists(status.itemPath))
                    throw new InvalidOperationException("目标 Item 路径被其他类型资产占用：" + status.itemPath);
                bool isNew = item == null;
                bool metadataChanged = previous == null || previous.status == "error" || previous.metadataHash != status.metadataHash;
                bool modelChanged = previous == null || previous.status == "error" || previous.modelHash != status.modelHash ||
                    AssetDatabase.LoadAssetAtPath<GameObject>(status.prefabPath) == null;
                if (!isNew && item.itemId != meta.itemId)
                    throw new InvalidOperationException("Item ID 冲突；保留现有资产并停止：" + status.itemPath);
                if (!isNew && item.prefab != null && AssetDatabase.GetAssetPath(item.prefab) != status.prefabPath)
                    throw new InvalidOperationException("Item 已引用自定义 prefab；请为此图片选择新的名称/ID，避免覆盖手工配置。");
                CheckDuplicateId(meta.itemId, status.itemPath);
                Vector2 normalized = Vector2.one;
                var prefab = modelChanged ? UpsertPrefab(model, status.prefabPath, out normalized) :
                    AssetDatabase.LoadAssetAtPath<GameObject>(status.prefabPath);
                if (isNew) item = ScriptableObject.CreateInstance<PlaceableItemData>();
                bool dirty = isNew;
                if (isNew || (meta.updateMetadata && metadataChanged))
                {
                    ApplyMetadata(item, meta, isNew, normalized);
                    dirty = true;
                }
                if (item.prefab != prefab) { item.prefab = prefab; dirty = true; }
                // Missing/disabled cutout never erases an existing designer icon.
                if (icon != null && item.icon != icon) { item.icon = icon; dirty = true; }
                if (isNew) AssetDatabase.CreateAsset(item, status.itemPath);
                if (dirty) { EditorUtility.SetDirty(item); AssetDatabase.SaveAssets(); }
                string catalogStatus = SyncScene(status.sceneAssetPath, item, modelChanged || dirty ||
                    previous == null || previous.iconHash != status.iconHash, out string message);
                if (item.category == ItemCategory.Terrain || item.category == ItemCategory.Animal)
                {
                    catalogStatus = "pending_visibility";
                    message += " 当前游戏的 Terrain 分页为地形工具，Animal 没有道具分页；要显示为道具请使用 Decoration/Plant/Building。";
                }
                status.status = catalogStatus;
                status.message = message;
                // Texture import settings can change its dependency hash during this operation.
                status.iconHash = string.IsNullOrEmpty(status.iconPath) ? "" : DependencyHash(status.iconPath);
                WriteStatus(status);
                if (previous == null || previous.metadataHash != status.metadataHash || previous.status != status.status ||
                    previous.modelHash != status.modelHash || previous.iconHash != status.iconHash)
                    Debug.Log("[ImagePartsTo3D] " + meta.itemId + ": " + status.status + " — " + message, item);
                return false;
            }
            catch (Exception ex)
            {
                status.status = "error";
                status.message = ex.Message;
                if (mayWriteStatus && !string.IsNullOrEmpty(status.itemId)) WriteStatus(status);
                int attempt = Pending.TryGetValue(metadataPath, out int n) ? n : 0;
                if (attempt == 0 || attempt == 7)
                    Debug.LogWarning("[ImagePartsTo3D] " + metadataPath + ": " + ex.Message);
                return ex is IOException;
            }
        }

        static void Validate(PropMetadata meta, string path)
        {
            if (meta == null || meta.schemaVersion != 1) throw new InvalidDataException("不支持的 .prop.json schemaVersion。");
            if (string.IsNullOrWhiteSpace(meta.itemId) || SanitizeId(meta.itemId) != meta.itemId)
                throw new InvalidDataException("itemId 必须为规范化的稳定 ID。");
            if (string.IsNullOrEmpty(meta.modelFile) || Path.GetFileName(meta.modelFile) != meta.modelFile ||
                !meta.modelFile.EndsWith(".glb", StringComparison.OrdinalIgnoreCase) ||
                SanitizeId(Path.GetFileNameWithoutExtension(meta.modelFile)) != meta.itemId)
                throw new InvalidDataException("modelFile 必须为当前目录中的 GLB 文件名，规范化后与 itemId 一致。");
            if (!path.EndsWith(Path.GetFileNameWithoutExtension(meta.modelFile) + ".prop.json", StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("元数据文件必须与 GLB 同名：<model>.prop.json。");
            if (!string.IsNullOrEmpty(meta.iconAssetPath))
            {
                AssetPath(meta.iconAssetPath);
                if (!meta.iconAssetPath.EndsWith(".png", StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("图标必须是 PNG。");
            }
            if (!string.IsNullOrEmpty(meta.sceneAssetPath))
            {
                AssetPath(meta.sceneAssetPath);
                if (!meta.sceneAssetPath.EndsWith(".unity", StringComparison.OrdinalIgnoreCase)) throw new InvalidDataException("sceneAssetPath 必须为 Unity 场景。");
            }
            if (meta.price < -1 || float.IsNaN(meta.worldScale) || float.IsInfinity(meta.worldScale) ||
                meta.worldScale < 0 || (meta.worldScale > 0 && meta.worldScale < 0.1f)) throw new InvalidDataException("无效的 price/worldScale。");
            if (meta.footprint != null && (meta.footprint.Length != 2 || meta.footprint.Any(v => v < 1)))
                throw new InvalidDataException("footprint 必须为两个正整数 [x,z]。");
            if (meta.updateFields != null && meta.updateFields.Any(field =>
                !new[] { "displayName", "category", "price", "worldScale", "footprint" }.Contains(field)))
                throw new InvalidDataException("updateFields 包含不支持的设计字段。");
            if (!string.IsNullOrEmpty(meta.category) && (!Enum.TryParse(meta.category, out ItemCategory cat) || !Enum.IsDefined(typeof(ItemCategory), cat)))
                throw new InvalidDataException("无效的 category。");
        }

        static void ApplyMetadata(PlaceableItemData item, PropMetadata meta, bool isNew, Vector2 normalized)
        {
            // A copied sidecar also retains prior defaults. Only this command's explicit
            // field list may override an existing designer-edited ItemData.
            Func<string, bool> apply = field => isNew || meta.updateFields == null || meta.updateFields.Contains(field);
            if (isNew)
            {
                item.itemId = meta.itemId;
                item.displayName = meta.itemId.Replace('_', ' ');
                item.category = InferCategory(meta.itemId);
                item.price = item.category == ItemCategory.Building ? 40 : item.category == ItemCategory.Plant ? 8 : 12;
                item.worldScale = 3;
                item.footprint = new Vector2Int(Mathf.Clamp(Mathf.RoundToInt(normalized.x * 3), 1, 3),
                                               Mathf.Clamp(Mathf.RoundToInt(normalized.y * 3), 1, 3));
            }
            if (apply("displayName") && !string.IsNullOrWhiteSpace(meta.displayName)) item.displayName = meta.displayName;
            if (apply("category") && !string.IsNullOrEmpty(meta.category)) item.category = (ItemCategory)Enum.Parse(typeof(ItemCategory), meta.category);
            if (isNew && meta.price < 0) item.price = item.category == ItemCategory.Building ? 40 : item.category == ItemCategory.Plant ? 8 : 12;
            if (apply("price") && meta.price >= 0) item.price = meta.price;
            if (apply("worldScale") && meta.worldScale >= 0.1f) item.worldScale = meta.worldScale;
            if (apply("footprint") && meta.footprint != null) item.footprint = new Vector2Int(meta.footprint[0], meta.footprint[1]);
            else if (isNew) item.footprint = new Vector2Int(Mathf.Clamp(Mathf.RoundToInt(normalized.x * item.worldScale), 1, 3),
                                                          Mathf.Clamp(Mathf.RoundToInt(normalized.y * item.worldScale), 1, 3));
        }

        static ItemCategory InferCategory(string id)
        {
            if (new[] { "treehouse", "house", "hut", "cabin", "tower", "bridge", "stall", "windmill", "platform", "dock", "屋", "桥" }.Any(id.Contains))
                return ItemCategory.Building;
            if (new[] { "tree", "palm", "bush", "plant", "grass", "flower", "mushroom", "树", "花", "草" }.Any(id.Contains))
                return ItemCategory.Plant;
            return ItemCategory.Decoration; // Terrain is a sculpting toolbar in this game.
        }

        static GameObject UpsertPrefab(GameObject model, string path, out Vector2 normalized)
        {
            var existing = AssetDatabase.LoadAssetAtPath<GameObject>(path);
            if (existing == null && File.Exists(path)) throw new InvalidOperationException("Prefab 路径被其他资产占用。");
            Scene preview = default;
            GameObject root = null;
            normalized = Vector2.one;
            try
            {
                if (existing != null) root = PrefabUtility.LoadPrefabContents(path);
                else
                {
                    preview = EditorSceneManager.NewPreviewScene();
                    root = new GameObject(Path.GetFileNameWithoutExtension(path));
                    SceneManager.MoveGameObjectToScene(root, preview);
                }
                var previousModel = root.transform.Find(ManagedModel);
                bool legacy = previousModel == null && existing != null;
                if (legacy)
                {
                    previousModel = root.transform.Find("Model");
                    if (previousModel == null) throw new InvalidOperationException("现有 prefab 不是受支持的生成结构；已保留，请换一个 ID。");
                }
                Vector3 authoredScale = root.transform.localScale;
                Vector3 position = root.transform.position;
                Quaternion rotation = root.transform.rotation;
                root.transform.SetPositionAndRotation(Vector3.zero, Quaternion.identity);
                root.transform.localScale = Vector3.one;
                // Upgrade legacy normalization, preserving any additional designer scale multiplier.
                if (legacy)
                {
                    float legacyDimension = MaxDimension(BoundsFor(previousModel.gameObject).size);
                    authoredScale *= Mathf.Max(legacyDimension, 0.0001f);
                }
                if (previousModel != null) Object.DestroyImmediate(previousModel.gameObject);
                var container = new GameObject(ManagedModel);
                container.transform.SetParent(root.transform, false);
                var instance = PrefabUtility.InstantiatePrefab(model, root.scene) as GameObject;
                if (instance == null)
                {
                    instance = Object.Instantiate(model);
                    SceneManager.MoveGameObjectToScene(instance, root.scene);
                }
                instance.name = "SourceGLB";
                instance.transform.SetParent(container.transform, false);
                // Preserve the imported root rotation AND scale (GLB coordinate conversion).
                instance.transform.localPosition = Vector3.zero;
                var bounds = BoundsFor(instance);
                float dimension = MaxDimension(bounds.size);
                if (dimension <= 0.0001f) throw new InvalidDataException("GLB 的网格尺寸为零。");
                instance.transform.localPosition = new Vector3(-bounds.center.x, -bounds.min.y, -bounds.center.z);
                container.transform.localScale = Vector3.one / dimension;
                normalized = new Vector2(bounds.size.x / dimension, bounds.size.z / dimension);
                root.transform.SetPositionAndRotation(position, rotation);
                root.transform.localScale = existing == null ? Vector3.one : authoredScale;
                var saved = PrefabUtility.SaveAsPrefabAsset(root, path, out bool success);
                if (!success || saved == null) throw new IOException("保存 prefab 失败：" + path);
                return saved;
            }
            finally
            {
                if (root != null && existing != null) PrefabUtility.UnloadPrefabContents(root);
                if (preview.IsValid()) EditorSceneManager.ClosePreviewScene(preview);
            }
        }

        static Bounds BoundsFor(GameObject go)
        {
            var renderers = go.GetComponentsInChildren<Renderer>(true);
            if (renderers.Length == 0) throw new InvalidDataException("GLB 没有可见网格。");
            Bounds result = renderers[0].bounds;
            foreach (var renderer in renderers.Skip(1)) result.Encapsulate(renderer.bounds);
            return result;
        }
        static float MaxDimension(Vector3 value) => Mathf.Max(value.x, value.y, value.z);

        static Sprite LoadIcon(string path)
        {
            if (!File.Exists(path)) throw new IOException("去背图尚未复制完成：" + path);
            var importer = AssetImporter.GetAtPath(path) as TextureImporter;
            if (importer == null) { AssetDatabase.ImportAsset(path, ImportAssetOptions.ForceSynchronousImport); importer = AssetImporter.GetAtPath(path) as TextureImporter; }
            if (importer == null) throw new IOException("无法载入 PNG 的 TextureImporter：" + path);
            if (importer.textureType != TextureImporterType.Sprite || importer.spriteImportMode != SpriteImportMode.Single ||
                importer.alphaSource != TextureImporterAlphaSource.FromInput || !importer.alphaIsTransparency ||
                importer.mipmapEnabled || importer.textureCompression != TextureImporterCompression.Uncompressed)
            {
                importer.textureType = TextureImporterType.Sprite;
                importer.spriteImportMode = SpriteImportMode.Single;
                importer.alphaSource = TextureImporterAlphaSource.FromInput;
                importer.alphaIsTransparency = true;
                importer.mipmapEnabled = false;
                importer.textureCompression = TextureImporterCompression.Uncompressed;
                importer.SaveAndReimport();
            }
            var sprite = AssetDatabase.LoadAssetAtPath<Sprite>(path);
            if (sprite == null) throw new IOException("PNG 尚未导入为 Sprite：" + path);
            var main = AssetDatabase.LoadMainAssetAtPath(path);
            var labels = AssetDatabase.GetLabels(main);
            if (!labels.Contains("ImagePartsTo3D.SourceCutout"))
                AssetDatabase.SetLabels(main, labels.Concat(new[] { "ImagePartsTo3D.SourceCutout" }).ToArray());
            return sprite;
        }

        static string SyncScene(string path, PlaceableItemData item, bool refresh, out string message)
        {
            message = "模型、去背 Sprite、道具配置和两个道具目录已同步。";
            if (!File.Exists(path)) { message = "已生成资产，等待指定场景存在：" + path; return "pending_catalog"; }
            Scene active = SceneManager.GetActiveScene();
            Scene scene = SceneManager.GetSceneByPath(path);
            bool opened = !scene.IsValid() || !scene.isLoaded;
            if (opened) scene = EditorSceneManager.OpenScene(path, OpenSceneMode.Additive);
            bool wasDirty = scene.isDirty;
            try
            {
                var roots = scene.GetRootGameObjects();
                var placements = roots.SelectMany(r => r.GetComponentsInChildren<PlacementController>(true)).ToArray();
                var toolbars = roots.SelectMany(r => r.GetComponentsInChildren<IslandBuildController>(true)).ToArray();
                if (placements.Length == 0 || toolbars.Length == 0)
                {
                    message = "场景缺少 PlacementController 或 IslandBuildController；已保留生成资产。";
                    return "pending_catalog";
                }
                bool changed = false;
                foreach (var placement in placements) changed |= AddToCatalog(placement, "catalog", item);
                foreach (var toolbar in toolbars)
                {
                    changed |= AddToCatalog(toolbar, "gameplayCatalog", item);
                    if (refresh || changed)
                    {
                        // Refresh only slots. Avoid Build(), which reconstructs the whole game scene.
                        var method = typeof(IslandBuildController).GetMethod("RefreshAssetSlots",
                            System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
                        if (method == null) throw new InvalidOperationException("当前工具栏脚本缺少 RefreshAssetSlots；需要适配新接口。");
                        method.Invoke(toolbar, null);
                        EditorUtility.SetDirty(toolbar);
                        changed = true;
                    }
                }
                if (changed) EditorSceneManager.MarkSceneDirty(scene);
                if (!opened && wasDirty)
                {
                    message = "道具目录已更新到当前场景；场景原本有未保存修改，请正常保存场景完成持久化。";
                    return "pending_scene_save";
                }
                if (changed && !EditorSceneManager.SaveScene(scene)) throw new IOException("道具目录场景保存失败：" + path);
                return "registered";
            }
            finally
            {
                if (opened && scene.IsValid()) EditorSceneManager.CloseScene(scene, true);
                if (active.IsValid() && active.isLoaded) SceneManager.SetActiveScene(active);
            }
        }

        static bool AddToCatalog(Object owner, string field, PlaceableItemData item)
        {
            var serialized = new SerializedObject(owner);
            var catalog = serialized.FindProperty(field);
            if (catalog == null || !catalog.isArray) throw new InvalidOperationException("缺少序列化字段：" + owner.GetType().Name + "." + field);
            for (int i = 0; i < catalog.arraySize; i++)
            {
                var existing = catalog.GetArrayElementAtIndex(i).objectReferenceValue as PlaceableItemData;
                if (existing == item) return false;
                if (existing != null && existing.itemId == item.itemId) throw new InvalidOperationException("目录中已有其他相同 itemId 资产：" + item.itemId);
            }
            int index = catalog.arraySize;
            catalog.InsertArrayElementAtIndex(index);
            catalog.GetArrayElementAtIndex(index).objectReferenceValue = item;
            return serialized.ApplyModifiedProperties();
        }

        static void CheckDuplicateId(string id, string itemPath)
        {
            foreach (string guid in AssetDatabase.FindAssets("t:PlaceableItemData"))
            {
                string path = AssetDatabase.GUIDToAssetPath(guid);
                if (path == itemPath) continue;
                var item = AssetDatabase.LoadAssetAtPath<PlaceableItemData>(path);
                if (item != null && item.itemId == id) throw new InvalidOperationException("其他资产占用了 itemId：" + path);
            }
        }
        static GameObject LoadModel(string path) => AssetDatabase.LoadAssetAtPath<GameObject>(path) ??
            AssetDatabase.LoadAllAssetsAtPath(path).OfType<GameObject>().FirstOrDefault(go => go.transform.parent == null);
        static string SanitizeId(string value)
        {
            var result = new StringBuilder();
            foreach (char c in value.Trim().ToLowerInvariant())
            {
                if (char.IsLetterOrDigit(c) || c == '_') result.Append(c);
                else if (c == ' ' || c == '-' || c == '.') result.Append('_');
            }
            return result.ToString().Trim('_');
        }
        static string AssetPath(string path)
        {
            string normalized = path.Replace('\\', '/');
            if (!normalized.StartsWith("Assets/", StringComparison.Ordinal) || normalized.Split('/').Any(s => s == ".." || s == "." || s == ""))
                throw new InvalidDataException("路径必须位于工程 Assets 内且不能包含相对跳转：" + path);
            return normalized;
        }
        static void EnsureFolder(string folder)
        {
            string current = "Assets";
            foreach (string segment in folder.Substring(7).Split('/'))
            {
                if (!AssetDatabase.IsValidFolder(current + "/" + segment)) AssetDatabase.CreateFolder(current, segment);
                current += "/" + segment;
            }
        }
        static string FileHash(string path)
        {
            using (var hash = SHA256.Create()) using (var stream = File.OpenRead(path))
                return BitConverter.ToString(hash.ComputeHash(stream)).Replace("-", "").ToLowerInvariant();
        }
        static string DependencyHash(string path)
        {
            if (!File.Exists(path)) throw new IOException("文件尚未准备好：" + path);
            return AssetDatabase.GetAssetDependencyHash(path).ToString();
        }
        static PropImportStatus ReadStatus(string id)
        {
            string path = StateRoot + "/" + id + ".status.json";
            try { return File.Exists(path) ? JsonUtility.FromJson<PropImportStatus>(File.ReadAllText(path)) : null; }
            catch { return null; }
        }
        static void WriteStatus(PropImportStatus status)
        {
            Directory.CreateDirectory(StateRoot);
            status.utc = DateTime.UtcNow.ToString("o");
            string path = StateRoot + "/" + status.itemId + ".status.json";
            string temporary = path + ".tmp";
            File.WriteAllText(temporary, JsonUtility.ToJson(status, true), new UTF8Encoding(false));
            if (File.Exists(path)) File.Replace(temporary, path, null); else File.Move(temporary, path);
        }
    }

    internal sealed class ImagePartsTo3DAssetWatcher : AssetPostprocessor
    {
        public override int GetPostprocessOrder() => -1000;
        static void OnPostprocessAllAssets(string[] imported, string[] deleted, string[] moved, string[] movedFrom)
        {
            ImagePartsTo3DImporter.AssetsChanged(imported, deleted, moved);
        }
    }
}
#endif
