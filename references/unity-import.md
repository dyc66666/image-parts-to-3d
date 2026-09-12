# Unity 生物岛道具栏接入

本包为已存在的 AnimalHome / DropBy 生物岛建造游戏提供导入桥接。执行 `--unity-copy` 会准备 GLB、第一轮去背后的透明 PNG、同名元数据，并安装随包提供的 Editor 导入脚本。只有 Unity 完成资源导入、prefab / ItemData / 场景道具目录同步后，才能把状态称为已注册。仅仅复制成功不等于工具栏已可用。

## 已核对的实际工程

对 `F:/unity/My project` 进行了只读检查：

- Unity `6000.3.22f1`，`com.unity.cloud.gltfast` `6.14.1` 已在 `Packages/manifest.json` 中。
- `Assets/AnimalHome/Scripts/PlaceableItemData.cs` 包含 `itemId`、`displayName`、`category`、`price`、`icon`、`prefab`、`footprint`、`worldScale`、`allowStacking`。
- 放置控制器使用 `PlacementController.catalog`，`FindItem(itemId)` 查到配置后实例化 `item.prefab`。
- 工具栏控制器还维护自己的 `IslandBuildController.gameplayCatalog`。只更新放置控制器会造成目录不一致。
- 原工程的综合构建器实际操作的场景为 **`Assets/DropBY.unity`**。
- 原 `DropByScenePropImporter` 仅导入没有 ItemData 的 GLB，仅监听 GLB，不会更新旧模型或处理晚到的 PNG。
- 原图标生成器在所有物品已使用去背图时直接返回，不调用 `Completed`；依赖该回调来重建工具栏的原导入流程可能不继续。
- 原 `Build()` 会打开场景并修改多项玩法、场景和 UI 配置。本桥接直接更新两个目录以及现有工具栏槽位。

打包和离线测试不提交混元任务。实际执行 `--unity-copy` 时才按下文说明安装脚本并复制资源；没有已生成 GLB 时不会宣称道具已注册。

## 默认目录

```text
Assets/DropBy/Scripts/Editor/ImagePartsTo3DImporter.cs
Assets/AnimalHome/Imported3D/SceneProps/<name>.glb
Assets/AnimalHome/Imported3D/SceneProps/<name>.prop.json
Assets/AnimalHome/Icons/SourceCutouts/<name>.png
Assets/AnimalHome/Prefabs/AH_<itemId>.prefab
Assets/AnimalHome/Data/Items/<itemId>.asset
```

`<name>.png` 直接来自对应部件第一轮去背的透明 cutout，和提交给混元的白底输入拥有相同构图。它不会替换成混元预览图。Unity 把 PNG 设为 Single Sprite、使用原 alpha、关闭 mipmap，并作为 `item.icon`；拖拽产生的是 `item.prefab` 中的 GLB 模型。

使用 `--unity-project` 切换工程；`--unity-dir` 和 `--unity-icon-dir` 必须在同一工程的 Assets 内；`--unity-scene` 指定该工程内的 `.unity` 场景。若选择只有文件复制的 `--unity-no-importer`，不承诺自动注册。

## 首次安装和更新

复制助手识别目标工程的三个运行时脚本后，将桥接脚本放进 Editor 目录。已安装脚本内容不同则停止；明确选择 `--unity-update-importer` 时先备份再替换。已有 `.meta` 不覆盖，以保留 GUID。

安装器还对已识别的旧脚本作两个小范围修改，并保留备份：

1. 旧 `ImportNewProps()` 跳过带 `.prop.json` 或 `.prop.pending` 的模型，避免两套导入器竞争；普通裸 GLB 仍走原流程和菜单。
2. 旧模型图标生成器跳过带 `ImagePartsTo3D.SourceCutout` 标签的 PNG，以保护自定义图标目录中的去背图。

如果 Unity 正在运行，首次安装或脚本升级先只安装脚本，等待编译完成。桥接在成功加载后写入 `Library/ImagePartsTo3D/importer-ready.json`，其中 `bridgeHash`、`legacyHash`、`generatorHash` 是三个源码文件的 SHA-256。复制助手核对匹配后才允许继续；未就绪时在付费生成前停止，编译后重跑即可。Unity 未开启时可准备好资源，下次打开工程后处理。

复制顺序：先写 `<name>.prop.pending` → 原子替换 PNG → 原子替换 GLB → 最后原子提交 `.prop.json` → 移除 pending。旧元数据更新期间也不会导入半套文件。失败保留 pending 和本地成品；修正问题后重新执行导出，无需重新调用混元。

## 元数据协议 v1

每个 GLB 一个同目录、同文件名主干的 `.prop.json`：

```json
{
  "schemaVersion": 1,
  "itemId": "bamboo_ladder",
  "modelFile": "bamboo_ladder.glb",
  "iconAssetPath": "Assets/AnimalHome/Icons/SourceCutouts/bamboo_ladder.png",
  "displayName": "竹梯",
  "category": "Decoration",
  "price": 12,
  "worldScale": 3.0,
  "footprint": [1, 1],
  "updateMetadata": false,
  "updateFields": [],
  "sceneAssetPath": "Assets/DropBY.unity"
}
```

| 字段 | 规则 |
|---|---|
| `schemaVersion` | 固定为 1 |
| `itemId` | 稳定唯一 ID，必须等于 GLB 主干按旧导入器规则规范化的结果；不包含路径 |
| `modelFile` | 当前目录内的 GLB 文件名；不能是绝对路径或跨目录路径 |
| `iconAssetPath` | 工程 Assets 相对 PNG 路径；省略或空字符串表示不更新已有图标 |
| `displayName` | 可省略；新物品默认把 ID 的下划线转为空格 |
| `category` | 可省略；合法枚举 Plant、Building、Decoration、Terrain、Animal |
| `price` | 可省略；`-1` 表示默认；显式值为非负整数 |
| `worldScale` | 可省略或 0 表示默认；显式值至少 0.1，默认 3 |
| `footprint` | 可省略；两个正整数 `[宽,深]`，单位是工程的网格格数 |
| `updateMetadata` | 默认 false；true 才把本次显式字段应用到旧物品 |
| `updateFields` | 本次命令显式提供的设计字段名数组；只允许 displayName、category、price、worldScale、footprint。旧物品只更新列出的字段，空数组不覆盖任何设计字段；手写旧版 JSON 省略本字段时，沿用更新所有已提供字段的行为 |
| `sceneAssetPath` | 默认 `Assets/DropBY.unity`；必须位于当前工程 Assets 中 |

ID 规则和原 C# 导入器一致：转小写，保留 BMP Unicode 字母、十进制数字、下划线；空格、连字符、点转下划线；其他字符去掉；去掉首尾下划线。不同文件得到同一 ID 时必须改名，不能互相覆盖。

新物品默认 Building 价格 40、Plant 价格 8、Decoration 价格 12。房屋关键词先于树木关键词；没有匹配时为 Decoration。**石块也默认 Decoration**：实际游戏的 Terrain 分页已经被地形塑形工具占用，Terrain 道具不会显示；Animal 没有道具分页。显式 Terrain/Animal 仍保留，但完成报告会给出 `pending_visibility`，应修改分类或扩展游戏分页后再验收。

默认占地沿用旧导入器的归一化尺寸 × worldScale、四舍五入并限制 1–3 格的规则。实际工程目前使用更细的网格；默认值属于初始配置，不保证精确包围几何体。需要严格占地时用 `--footprint X Z`，或在 ItemData 中按实际网格调好尺寸；已有手工占地不会被模型更新覆盖。

## 更新时保留什么

- 保留 ItemData 和 prefab 文件，直接保存更新，保留它们的 GUID 与目录引用。
- 默认保留旧 `displayName`、`price`、`category`、`footprint`、`worldScale`、`allowStacking`。只有显式更新元数据时覆盖 `updateFields` 中本次用户提供的字段；例如只传 `--price 25 --unity-update-metadata` 不会把旧侧文件中的分类、名称、占地覆盖回去。
- 更新 GLB 管理的 `ImagePartsTo3D_Model` 子树，保持原始 GLB 的根旋转与缩放，重新居中、贴地、按最大边归一化。旧导入器的 `Model` 子树可升级为该结构。
- prefab 根节点的自定义组件、额外兄弟节点和根变换保留；受管理模型子树中的手工修改会随源模型更新重建。需要长期自定义的内容放在根节点或管理子树外。
- 若现有 ItemData 引用了其他自定义 prefab，或目标 prefab 不符合已知生成结构，停止并报告冲突；选择新名称/ID 后重试。
- PNG 更新会更新 Sprite 和工具栏；使用 `--no-icon` 不会删除或覆盖已有图标。新物品没有 PNG 时可能无图标；桥接不主动渲染替代图，可手动使用原模型图标工具。
- 只向两个目录追加缺失引用，不清空已有物品，不重复追加。未修改的模型用依赖指纹跳过重复重建。

## Unity 完成状态

自动入口监听元数据、GLB、PNG，并在脚本加载、返回编辑模式、场景打开或保存后检查。遇到模型或图片尚未导入等暂时性问题会重试。不能在播放或编译中写资产；后续回到编辑状态会继续。

可手动执行：**Tools → Drop By → 图片转3D → 同步模型、原图图标和道具栏**。

每个 ID 的结果在 `Library/ImagePartsTo3D/<itemId>.status.json`：

| `status` | 含义 |
|---|---|
| `registered` | 模型、配置、两个目录与目标场景同步并保存 |
| `pending_catalog` | 模型和配置已准备，但场景不存在或缺少目标控制器 |
| `pending_scene_save` | 当前场景本来有未保存修改；已更新内存中的目录，需要用户正常保存场景 |
| `pending_visibility` | 分类在当前游戏道具分页不可见，调整分类或游戏 UI |
| `error` | 读取、GLB 导入、路径、ID 或资产冲突；查看 `message` 与 Unity Console |

状态还包含 `metadataPath`、`modelPath`、`iconPath`、`prefabPath`、`itemPath`、`sceneAssetPath`、`utc` 和三个内容/依赖指纹。Unity 没启动或尚未导入时没有完成报告；命令行此时只应报告 copied/pending。

目标场景没打开时以 Additive 方式暂时加载，保存本次目录变更后关闭，恢复原活动场景。已经打开且存在未保存修改时不自动保存，以免一起保存尚未确认的其他修改。

## 验收和边界

交付前已用实际 Unity 6 和当前游戏程序集编译桥接 C#，无编译错误。未在真实编辑器执行资产导入、UI 渲染或放置操作；以下为安装后的验收步骤：

1. 用一个单道具输入生成或使用本地产物重试导出，等 Unity Console 无错误。
2. 检查 ItemData：icon 指向本轮去背 PNG，prefab 指向 AH_ 对应模型；两个目录中都只有一条该物品引用。
3. 状态为 `registered` 后，在相应分页找到图标，拖到岛屿上确认落地为 3D 模型、方向正确、贴地、比例与占地合理。
4. 修改名称/价格/占地后，用同 ID 更新 GLB / PNG；默认应保留这些手工值，模型和图标更新。
5. 把目标场景留为未保存修改状态再导入，状态应为 `pending_scene_save`；正常保存后恢复注册状态。
6. 自定义图标目录后手动运行原模型图标生成器，标签保护应保留去背图标。

Unity Prefab 保存会尝试保留对象引用，但内部同名对象的匹配有局限；不要依赖生成模型内部节点作为稳定的手工引用目标。[Unity Prefab 保存文档](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/PrefabUtility.SaveAsPrefabAsset.html)

PNG 使用 Single Sprite 的导入设置；GLB 的编辑器导入依赖 glTFast。目标工程更新包或改动运行时字段后，需要重新编译并检查适配接口。[Unity Sprite 导入设置](https://docs.unity3d.com/6000.0/Documentation/ScriptReference/TextureImporter-spriteImportMode.html) · [glTFast 编辑器导入](https://docs.unity3d.com/Packages/com.unity.cloud.gltfast@6.14/manual/ImportEditor.html)

元数据未提供的字段按当前 Unity 6.3 的反序列化规则保留字段初始化值（例如 price=-1），与本协议的“使用默认”含义一致。[Unity JsonUtility.FromJson](https://docs.unity3d.com/6000.3/Documentation/ScriptReference/JsonUtility.FromJson.html)
