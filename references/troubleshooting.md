# 故障排查

## 图片与分割

| 症状 | 处理 |
|---|---|
| 单个道具被拆成多个模型 | 使用 `--no-split`；它会保留不连通的横杆、树叶等部分 |
| 多物体相接、没有拆开 | 连通域无法识别语义边界；分别裁剪后生成，或明确作为组合道具 |
| 背景残留 | 纯色图可适当增大 `--threshold`；复杂场景先 AI 去背 |
| 主体被削掉 | 减小颜色容差；已有透明 PNG 应显示 `bg_removal=alpha` |
| `NEEDS_AI_CUTOUT` / 退出码 2 | 当前图片背景复杂且 Vision 不可用；按 SKILL.md 先 AI 去背后重跑 |
| 图标有另一个物件 | 检查是不是输入中相接的对象；新版独立连通域使用独立蒙版，避免包围盒交叠造成混入 |
| 全透明输入 | 不提交生成；改用包含可见道具的输入 |

`--dry-run` 会返回实际 `_prepared_*` 目录，查看其中 `parts` 与 `cutouts`。不要依赖旧版固定的 `_prepared` 路径。原始透明 PNG 不会再被 RGB 转换丢掉 alpha。

## 生成与下载

| 症状 | 处理 |
|---|---|
| 找不到 buddy-cloud.py | 配置 `BUDDY_CLOUD_SCRIPT`，或在具备 WorkBuddy/CodeBuddy 客户端的环境运行 |
| 缺少/过期 token | 由 WorkBuddy 调用 `connect_cloud_service` 获取新的 tempToken，再经 `--token-stdin` 传入 |
| 并发/每日配额错误 | 查看服务当前额度；脚本停止后续提交并记录未提交的部件，不自动重做整批 |
| LowPoly + model 3.1 | 保留兼容行为，实际发送并记录 3.0 |
| 下载失败或结果无 GLB | 查看 manifest.failed；不会将旧 GLB 算成本轮成功。带 job_id 时先查询原任务并下载，不直接重复提交 |
| 预览图下载失败 | 可选预览失败不影响模型成品；工具栏用先前去背 PNG |
| 超时 | manifest 保留已提交 job_id 和未提交部件；远程任务可能仍运行，先查询 |
| `base64 后超过 6MB` | 减小输入或 `--size`；校验按编码后的大小执行 |
| 生成质量不足 | 改参考图或生成提示；Normal、LowPoly 等原模式保留。技能不会自动修网格或保证拓扑质量 |

## Unity 导入

先区分本地文件、复制状态、Unity 导入报告。详细状态、脚本安装和恢复方式见 [Unity 导入](unity-import.md)。

| 症状 | 处理 |
|---|---|
| Python 复制失败 | GLB/PNG 仍保留；修正路径、权限或占用后，用 `unity_export.py --manifest ... --unity-copy` 重试，不重新生成模型 |
| 首次安装/升级脚本时 Unity 正在运行 | 先等待脚本编译完成再重试导出，或关闭 Unity 导出后重开。就绪检查防止旧版监听器抢先处理 |
| `pending_unity_editor` | 文件已提交，等待 Editor 导入；该状态不是游戏内验收成功 |
| GLB 没变成 GameObject | 检查 glTFast 和 Console。核对的目标工程使用 `com.unity.cloud.gltfast` 6.14.1；其他版本/项目需按其版本配置 |
| 改 `--unity-dir` 后图标路径错误 | 新版从同一个目标工程推导图标目录；自定义路径也必须位于相同工程的 Assets 下 |
| 名称冲突 | 空格、横线、点会归一为下划线；不同道具使用不同的稳定文件名，不要让两个文件映射同一个 ID |
| 已有道具属性没改 | 默认保留手工设置；明确传入 `--unity-update-metadata` 和需要改的字段 |
| 模型/图标没更新 | 查看对应 `.status.json` 和 Unity Console；使用新桥接菜单重新同步，不需要重建整套游戏场景 |
| 源图图标被旧渲染工具覆盖 | 检查已安装的管理资源保护；桥接导入使用 sidecar 指定的 PNG 路径，包括自定义图标目录 |
| 新道具不显示 | 检查两个目录、目标场景和分类。当前 Terrain 页是地形工具，Animal 无普通道具页；一般道具用 Decoration / Plant / Building |
| 场景有未保存修改 | 桥接保留用户修改，正常保存场景后会再次同步；不要误认为目录已持久化 |
| 更新桥接脚本被拒绝 | 已安装文件与包中不同，先审查差异；确认更新时用 `--unity-update-importer`，原版会备份 |
| 只返回 FBX/STL/USDZ | 独立导出这些格式仍支持，但生物岛这条链路必须提供 GLB |

## 本地测试

```bash
python -m unittest discover -s tests -v
```

依赖缺失时安装 Pillow 和 numpy。测试不会调用真实混元服务、不会改动实际 Unity 工程。Unity C# 编译检查验证接口兼容性；真实材质、场景图标和拖拽放置仍需在 Editor 中验收。

WorkBuddy 中不要要求用户把临时凭证粘贴进聊天。脚本带 `--token-stdin` 时需要宿主传入标准输入并关闭输入流；手动只运行命令而未输入会等待。仅 dry-run 和本地 Unity 重试无需凭证。
