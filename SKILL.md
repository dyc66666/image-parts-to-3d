---
name: image-parts-to-3d
description: 将用户提供的道具裁剪图或多物件图片去背、分割，调用混元 3D 生成 GLB；保留先前去背图作为图标，并可将模型、图标和配置导入 Unity 生物岛建造游戏的道具栏。适用于图片生成 3D、导出 GLB，以及将生成道具接入 AnimalHome/DropBy 工程。
---

# 图片 → 3D → 生物岛道具栏

面向用户的安装与调用说明见 [WorkBuddy 使用说明](使用说明.md)。技能和脚本位置以解压后的实际技能目录为准，不依赖 Codex 工作目录。

将图片去背 → 分割 → 混元 3D → 保存成品 → 按需导入 Unity → 清理本次中间文件。这里的“清理”指临时图片和下载文件，不会自动修复拓扑、减面或重新制作模型。

用户给的是**一个道具的裁剪图**时，使用 `--no-split`，避免梯子横杆、树叶等不相连区域被误当多个道具。用户明确要拆开多个物件时才省略此参数。图标复用本轮最早的去背结果；3D 输入是同构图的白底图片。工具栏显示 PNG，拖拽放置使用 GLB 生成的 prefab。

## 执行环境与凭证

相对路径均相对于本技能目录。Python 需要 `numpy`、`Pillow`：

```bash
python -m pip install numpy Pillow
```

生成使用 `hunyuan3d.py`，依赖 WorkBuddy/CodeBuddy 自带的 `buddy-cloud.py`。WorkBuddy 中执行时：

1. 每次开始真实生成或恢复云端查询前，调用 `connect_cloud_service`（无参数）；若工具未直接列出，先用宿主的工具搜索找到它。
2. 使用返回的 `tempToken`，缺少时才用 `token`；通过 `--token-stdin` 将凭证传给流水线/独立生成脚本。不要把凭证发给用户、保存到文件或作为 `--token` 明文参数。
3. 脚本不会自行登录 WorkBuddy。若连接工具不可用或连接失败，明确报告该错误，不能只因客户端文件存在就声称已经获得授权；需要登录时由用户完成登录。
4. 不复用过期授权。保留 `BUDDY_CLOUD_TOKEN` 环境变量和旧 `--token` 接口供既有调用兼容；`--token-stdin` 优先。

客户端按 `BUDDY_CLOUD_SCRIPT`、WorkBuddy 插件缓存、原 CodeBuddy 路径顺序发现。仅自动发现失败时显式设置实际客户端路径。

Windows 通常安装到 `%USERPROFILE%/.workbuddy/skills/image-parts-to-3d/`。Python 使用宿主实际可用的解释器；本机 WorkBuddy 托管解释器是 `%USERPROFILE%/.workbuddy/binaries/python/versions/3.13.12/python.exe`，其他机器先检查实际版本，不硬编码不存在的路径。依赖应安装到执行流水线的同一个解释器。

`--dry-run`、图片预处理和已有产物的 Unity 复制重试均不需要云端授权。`--dry-run --token-stdin` 也不会等待输入凭证。

## 默认工作步骤

1. 先运行 `--dry-run`，检查输出的白底部件图、透明图和 `needs_ai_cutout`。检查主体完整、无场景背景、图标构图正确。已有透明 PNG 会保留原 alpha，不再按背景色重新抠掉主体。
2. 对复杂背景，Windows 没有 macOS Vision 时仍保留原有退出码 2 保护。先按下文 AI 去背回退处理，不要为了继续而擅自增加 `--allow-degraded`。
3. 单道具采用 `--no-split` 运行生成。保留并发上限 2、LowPoly 自动使用模型 3.0、配额触发停止后续提交的行为。
4. 成功后保存 GLB、每个部件同名透明 PNG、可选混元预览图、`.prop.json` 和 `manifest.json`。`--no-icon` 是用户明确不需要去背图标时的兼容选项。
5. 用户要求接入生物岛道具栏时加 `--unity-copy`。先预检工程/目录/导入脚本兼容性，再提交收费生成。首次安装或升级时若 Unity 已打开，预检会先安装必要 Editor 脚本并等待编译；收到就绪状态后重跑才开始生成。细节见 [Unity 导入](references/unity-import.md)。
6. 区分“本地生成完成”“已复制、等待 Unity”“Unity 已登记且目录可用”。只有检查 Unity 导入报告和实际道具栏后，才报告已在游戏内验证。不要把 Python 退出码 0 当成 Unity 运行验证。

检查去背：

```bash
python scripts/pipeline.py --input "prop.png" --work-dir "./3d-assets" --name bamboo_ladder --no-split --dry-run
```

生成并接入本机生物岛工程（命令中的示例路径按实际情况替换）：

```bash
python scripts/pipeline.py --input "prop.png" --work-dir "./3d-assets" --name bamboo_ladder --no-split --generate-type LowPoly --face-count 20000 --unity-copy --unity-project "F:/unity/My project" --display-name "竹梯" --category Decoration --token-stdin
```

只生成模型和可携带的本地产物时省略 `--unity-copy`。默认分割参数和原有独立脚本仍可使用。

## 输出及恢复

```text
3d-assets/bamboo_ladder/
├── bamboo_ladder.glb          # 最终模型
├── bamboo_ladder.png          # 最早去背结果的部件图，透明背景
├── bamboo_ladder.prop.json    # ID、模型/图标映射和可选游戏属性
├── bamboo_ladder_preview.png  # 混元预览，若服务提供；不是去背图标
└── manifest.json              # 来源、实际生成参数、job_id、结果、失败与 Unity 状态
```

多部件沿用 `<name>_part00`、`_part01` 命名，每个模型对应自己的透明图；分割时使用独立蒙版，避免另一物件落在包围盒内而混入图标或模型输入。GLB/PNG 保留原文件名；用于 Unity 的 itemId 会统一处理空格、大小写等，映射写入元数据。不同文件名归一成同一 ID 时会停止并报告冲突。

只在整次生成和请求的导出均成功时清理本次 `_prepared_*` 目录；失败或 `--keep-parts` 保留它。不会先删除上次的去背图。复制失败以非零状态报告，不吞掉错误，成功生成的模型无需重做。

用现有 manifest **只重试导入**：

```bash
python scripts/unity_export.py --manifest "./3d-assets/bamboo_ladder/manifest.json" --unity-copy --unity-project "F:/unity/My project"
```

重试会恢复 manifest 中保存的目标目录和道具属性，命令行显式参数优先；成功后同步更新 manifest 状态。也可用独立 GLB 与先前去背 PNG；参数见 `python scripts/unity_export.py --help`。不得将 GLB 渲染预览误作为去背图标。

## 参数

### 图片、生成和流程（pipeline.py）

| 参数 | 默认/用途 |
|---|---|
| `--input` | 必填，原图或先前去背 PNG |
| `--work-dir` / `--name` | `./3d-assets` / 图片名；name 为单层文件名 |
| `--no-split` | 保留一个整体；单道具截图推荐使用 |
| `--max-parts` / `--min-area` | 8 / 0.015；控制保留部件数和最小面积 |
| `--method` | auto / color / vision；透明输入直接保留 alpha |
| `--threshold` | 42；颜色去背容差，主体被削掉时调小 |
| `--size` / `--padding` / `--bg` | 1024 / 0.10 / 255,255,255；模型输入画布，图标仍透明 |
| `--generate-type` | LowPoly；也支持 Normal / Geometry / Sketch |
| `--model` | 3.0；支持 3.1，LowPoly 实际强制 3.0 |
| `--face-count` | 30000，服务支持范围应在执行时核对 |
| `--prompt` / `--enable-pbr` | 附加生成引导 / 请求 PBR |
| `--result-format` | 不设置时取服务默认 GLB 等；兼容 FBX / STL / USDZ。Unity 此链路必须有 GLB，若服务仅返回替代格式会明确失败 |
| `--dry-run` | 不调用生成、不安装脚本、不写入 Unity，保留检查图 |
| `--keep-parts` | 成功时也保留中间图片 |
| `--allow-degraded` | 用户明确接受颜色去背降级时才用 |
| `--poll-interval` / `--max-wait` | 15 秒 / 1800 秒；超时保留 job_id，远程任务可能仍在进行 |
| `--token-stdin` | WorkBuddy 推荐：由宿主将临时凭证传至标准输入 |
| `--token` | 仅保留兼容；新调用不使用明文参数传递凭证 |

### Unity 导出（共用 unity_export.py）

| 参数 | 用途 |
|---|---|
| `--unity-copy` | 同步模型、图标、元数据并安装所需 Editor 桥接脚本 |
| `--unity-project` | 工程根目录，默认 `F:/unity/My project` |
| `--unity-dir` | 覆盖 GLB 目录，必须位于目标工程 Assets 内 |
| `--unity-icon-dir` | 覆盖图标目录；默认从目标工程推导 `Assets/AnimalHome/Icons/SourceCutouts` |
| `--no-icon` | 不导出去背图；已有图标保留，新条目可能无图标，不承诺模型渲染回退 |
| `--item-id` / `--display-name` | 单道具的稳定 ID / 显示名称；ID 必须等于模型文件名规范化结果，多部件需逐件命名 |
| `--category` / `--price` | 可选分类 / 价格 |
| `--world-scale` / `--footprint` | 世界缩放至少 0.1 / 占地 `X Z` 两个正整数 |
| `--unity-update-metadata` | 显式覆盖已有条目的传入属性；默认保留设计者设置 |
| `--unity-update-importer` | 明确更新已有且内容不同的桥接脚本，避免静默覆盖手工修改 |
| `--unity-scene` / `--unity-no-importer` | 指定场景 / 仅复制，不安装自动注册桥接 |
| `--no-unity-copy` / `--with-icon` | 在重试时覆盖 manifest 保存的复制/图标开关 |

默认目录为 `Assets/AnimalHome/Imported3D/SceneProps` 和 `Assets/AnimalHome/Icons/SourceCutouts`。更换 `--unity-dir` 时图标不会继续复制到旧 F 盘工程。目标项目必须具备 AnimalHome/DropBy 数据和控制器；普通 Unity 工程可先保留本地产物，不能宣称自动获得生物岛 UI。

桥接脚本负责 Sprite 导入、以 GLB 建立居中贴地 prefab、创建/更新 PlaceableItemData、同步放置目录。保留已有 `.meta`/GUID 和设计者配置；为防止旧导入器抢先处理，在旧扫描循环内加入仅跳过本流水线管理资源的判断，保留原入口及非本流水线资源的处理。具体备份、报告及场景规则见 [Unity 导入](references/unity-import.md)。

## Windows 复杂背景与 AI 去背

颜色法只能可靠处理接近纯色背景。四角均匀度 >= 26 且 Vision 不可用时，非 dry-run 默认返回 `NEEDS_AI_CUTOUT`，不浪费 3D 生成次数。

使用宿主实际可用的图片编辑工具处理原始图，要求：“去除场景背景，只保留这个道具，透明背景，保持原来的结构、材质、配色和角度，不增加物件”。按该工具实际支持的参数调用，不假定固定 ImageGen 参数接口。若只能获得白底图，也可让颜色法后续处理。将成品放在稳定路径后重新传入 `--input`，保持同一个 `--name`。默认 PNG 图标来自这份去背图，不二次生成，不改成模型截图。必须实际检查 PNG 的 alpha；画出来的棋盘格不是透明背景。若输出只有假透明棋盘格，修正背景后重新检查，不提交给混元。

AI 去背不等于模型生成，可能独立收费。生成额度、积分价格和耗时取决于当前服务：原包记录过并发 2、每日 5 次、LowPoly 约 30 积分、Normal 约 20 积分，这些是历史环境值，不能当作所有账户的当前保证。执行前按实际服务核对，说明预计任务数；多部件按件提交。额度或超时失败不自动重新提交整批收费任务。

## 独立工具与检查

```bash
python scripts/prepare_parts.py --input photo.jpg --out-dir prepared --keep-cutout
python scripts/hunyuan3d.py submit --image prepared/parts/part_00.png --model 3.0 --generate-type LowPoly --token-stdin
python scripts/hunyuan3d.py query JOB_ID --out recovered_model --token-stdin
python -m unittest discover -s tests -v
```

带 `--token-stdin` 的生成命令需要 WorkBuddy 同时供给标准输入，不能只手动粘贴命令后无限等待。

独立预处理无需云端；独立查询可恢复已完成的任务，随后搭配保存的透明 PNG 用 `unity_export.py` 导入。离线测试使用合成图和模拟云端，不消耗生成额度；它们不代表实际 Unity 场景的视觉和拖拽验收。其他问题见 [故障排查](references/troubleshooting.md)。
