# 2D 关键点裁剪预览：任务汇总与接续说明

**整理日期：** 2026-07-13  
**当前分支：** `feature/keypoint-preview`  
**状态：** 功能与最终审查修复均已完成；单元测试、真实数据生成和三种浏览器视口验证通过。  
**协作约束：** 后续同时运行的 Agent 不超过 2 个；简单修改默认不使用 Agent。

## 1. 目标

针对以下外部数据集生成可直接双击打开的静态 HTML 复核页：

```text
/Users/penghaotian/Downloads/DATAS/SWIMMING/游泳6拼接1080P-2D关键点标注
```

页面按人物展示 COCO-17 裁剪图，并叠加骨架、可见关键点和红色精准关键点框；桌面四列，窄屏两列或一列；图片懒加载；卡片显示全局图片/人物序号，例如：

```text
图 23/54 · 人 156/554 · 164427_merged / frame_0012.png · ID 7
```

源数据只读，不复制进仓库，不修改标注或原图。

## 2. 当前实现文件

- `src/keypoint_preview.py`
  - 数据集解析与确定性排序
  - `Rect`、`PersonRecord`、`DatasetIndex`、`GenerationSummary`
  - 可见关键点、精准框和正方形裁剪几何
  - OpenCV 骨架、关键点和红框绘制
  - JPEG、`report.json` 和静态 `index.html` 生成
- `src/build_keypoint_preview.py`
  - 薄 CLI 入口
  - 参数：`--dataset-root`、`--output-dir`、`--padding-ratio`、`--minimum-side`
- `tests/test_keypoint_preview.py`
  - 几何、排序、解析、生成器、HTML 转义和 CLI 测试
- `tests/__init__.py`
- `README.md`
  - “检查 2D 关键点裁剪标注”使用说明
- `.gitignore`
  - 忽略 `outputs/keypoint_preview/`

生成产物位于：

```text
outputs/keypoint_preview/
├── crops/*.jpg
├── index.html
└── report.json
```

该目录被 Git 忽略。

## 3. 已验证的基线

在最终审查修复开始前，完整测试为 **11/11 通过**。

真实默认数据集曾成功运行：

```bash
.venv/bin/python src/build_keypoint_preview.py
```

结果：

```text
source images: 54
source persons: 554
generated crops: 553
skipped crops: 1
```

唯一跳过记录：

```json
{
  "person_index": 136,
  "image_index": 14,
  "session_name": "164427_merged",
  "frame_name": "frame_0008.png",
  "person_id": 16,
  "reason": "no_visible_keypoints"
}
```

当时还验证了：

- `generated + skipped == 554`
- `index.html` 包含 `IntersectionObserver`、`loading="lazy"` 和“红框：精准关键点框”
- 553 个 JPEG 裁剪文件，总体约 21 MB
- `git diff --check` 无错误
- 无头 Chrome 能通过 `file://` 打开页面并加载真实裁剪图

## 4. 最终审查修复

以下确认项均已修复并由回归测试覆盖：

1. `discover_dataset` 拒绝超出 `images` 范围的 `annotation.image_idx`，避免人物被静默丢弃。
2. `images[].file_name` 解析后必须仍位于所属 session 目录，拒绝绝对路径、`..` 和符号链接逃逸。
3. 关键点数值转换失败统一包装为 `DatasetFormatError`，CLI 不再泄漏 `TypeError`/`OverflowError` traceback。
4. `output_dir` 不得等于或位于 `dataset_root` 内，检查发生在任何写入前。
5. `cv2.imread` 抛出的 `cv2.error` 与返回 `None` 一样按 `unreadable_source_image` 记录并跳过。
6. `padding_ratio` 必须有限且非负，`minimum_side` 必须为正数。
7. 自然排序增加原始名称 tiebreaker，并保持 session-first、frame-second 的排序层级。
8. `<img>` 同时具有原生 `src` 和观察器使用的 `data-src`；JavaScript 禁用时仍能显示裁剪图。
9. 浮点裁剪使用 `floor(left/top)` 与 `ceil(right/bottom)` 推导包含边界的正方形栅格。
10. 所有 JPEG、HTML 和报告先生成到同级临时目录，成功后整体替换正式输出；失败保留上一版，成功重跑不会遗留旧 JPEG。

## 5. 非阻塞项

以下属于优化或超出已确认范围，暂不扩展：

- 卡片元数据同时存在于服务端 HTML 和内嵌 JSON，增加约 88 KB；内嵌 JSON 是原计划明确要求，暂不删除。
- 页面未做卡片虚拟化或滚动后卸载图片；当前 553 卡规模可用，需求只要求懒加载。
- 增量缓存、并行 JPEG 编码、全量 annotation 流式解析。
- 设计与计划中关于 session 纯字典序/自然排序的文字冲突；当前按既有计划和真实会话命名保持 session-first 自然排序，只修稳定性平局。

## 6. 最终验证结果

- `.venv/bin/python -m unittest tests.test_keypoint_preview -v`：**22/22 通过**。
- 真实数据运行：**54 张图、554 人、553 个裁剪、1 个跳过**，耗时约 7.7 秒。
- `outputs/keypoint_preview/crops/`：553 个 JPEG；报告统计一致。
- 临时与备份发布目录：生成结束后为 0。
- `git diff --check`：通过。
- Chrome `file://` 实测：
  - 1440px：首行 4 卡，首屏加载 24 张；
  - 900px：首行 2 卡，首屏加载 10 张；
  - 390px：首行 1 卡，首屏加载 5 张；
  - 三种视口均为 553 张卡、0 个失败卡、0 条浏览器运行时/日志错误。
- 禁用 JavaScript 后：553 张卡均可通过真实 `src` 加载，证明原生兜底生效。

## 7. 常用命令

```bash
# 完整测试
.venv/bin/python -m unittest tests.test_keypoint_preview -v

# 真实数据生成
.venv/bin/python src/build_keypoint_preview.py

# 报告统计
jq '.summary.source_image_count, .summary.source_person_count, .summary.generated_count, .summary.skipped_count' \
  outputs/keypoint_preview/report.json

# HTML 关键标记
rg -n 'IntersectionObserver|loading="lazy"|红框：精准关键点框' \
  outputs/keypoint_preview/index.html

# 差异格式
 git diff --check
```

## 8. Git 与协作约束

- 未经用户明确要求，不创建 commit，不 push。
- 保留当前源码、测试、README、`.gitignore` 和被忽略的生成产物。
- 本文替代本次任务原有的 Superpowers design、plan、brief、report、review diff、截图和进度台账。
- 后续 Agent 同时运行数量最多 2 个；本任务规模下优先直接实现和单次聚焦复核。
