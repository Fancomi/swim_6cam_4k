# 水面拉线视觉标定：新会话执行提示词

这是给执行助手的完整任务指令。用户只需指定原始照片目录，例如：

```text
请读取并执行 docs/prompts/water_entry_visual.md。
数据目录：D:\PROJECT\LABEL\datas\swim-water-entry-0911\0909
```

默认目标：**从镜头往远处数的第二条泳道**。用户明确指定其他泳道时，以用户为准。
默认模式：同一批原图命中已验收指纹时精确重建基准，再执行一轮指标复核与视觉修正；未命中时先视觉取点，再执行同样的复核。
用户可以追加 `模式：重新视觉取点，不读历史坐标`，用于检验纯视觉泛化质量。

## 任务与边界

你负责看完原图、辨认物理拉线、选择泳道、记录像素坐标，最终交付透明绘图、
带米数位置与 UV 的 FBX，以及可直接打开的 `index.html` 验收页。
完成实际文件和检查，不止给方案；无需为已规定的默认项反复询问用户。

这是 water_entry 下的独立视觉标定工作流。读仓库 `AGENTS.md`，保留现有四条链路。
入口 `python -m python.water_entry.visual_trial`，不改实时拼接、旧标定或相机对齐。
不需要旧设计师的 `ALL-01.jpg`、`20260901-qp-changchi`、旧 FBX、旧输出或聊天历史。
现有脚本与模板负责几何绘制，不需要 image generation/API key/FBX SDK。
用户允许脚本辅助裁剪、放大、拼接核对图和按坐标绘制；**主绳认定主要由你看图完成**。
不要换成前背景分离、Mask 融合或无人复核的自动线检测。

## 先确定工作模式

1. 枚举指定目录中的原图，检查文件名、图像尺寸及 SHA-256；不要修改原图。
2. 除非用户要求“重新视觉取点”，检查 `python/water_entry/visual_trials/*.json`。
   只有**整批文件名集合与每张图的 SHA-256 都一致**，才能复用那份已验收坐标。
   目录被搬家不影响匹配；同名照片内容变了、增删了照片，都不是同一批。
3. 命中且记录泳道与用户要求一致：运行下面的重建命令，保留坐标和绘制样式，不为“更美观”重新估计。
   仍打开一张原图和合成结果确认泳道位置，并查看验证结果。精确重建仅完成基准阶段，
   接着必须执行下文“指标复核与一轮修正”，不能因为 reference_matches 全通过就跳过。
4. 未命中或用户要求不同泳道：转入下面的“视觉取点”。不要尝试让 0909 坐标适配新图；
   不要求用户提供旧设计图。明确可用的输入可继续工作，只有泳道无法辨识、
   距离文件名没有明确含义或原图缺失等真正阻塞项才问一个具体问题。

```powershell
python -m python.water_entry.visual_trial --source "<用户提供的原图目录>"
```

0909 已验收记录是 `python/water_entry/visual_trials/0909.json`，
来源为 19 张 `0909-025.jpg` 到 `0909-475.jpg`，步进 025。
验收口径是第二条泳道；不是画面最下方 y≈627–713 的第一条道。
在这批图中，目标的远侧泳道绳约 y=591–599，近侧约 y=624–632。
这些像素范围只用于识别这批原图，不是新相机/新图的固定坐标。

## 视觉取点（新照片或用户要求独立重做）

1. 先看完整原图，按实际水面区域从近到远数泳道；最近的局部可见泳道也计数。
   用两根泳道绳之间的水面确定第二条道。不要按出发台编号替代数泳道。
   在进度消息中明确选择的两条边界和大致像素范围。
2. 看每一张照片，必要时对泳道区域做带原始坐标刻度的裁剪放大图。
   裁剪、缩放后所有坐标必须换回原图坐标；不能把显示缩略图坐标当原始像素。
3. 每張照片辨认这一位置横跨泳道的主绳，在**目标泳道内**选两个尽量分开的点。
   不能用邻道的点向目标泳道长距离外推。避开红白浮标边缘、池底线、瓷砖缝和倒影。
   主绳可能与较低、较淡的倒影成对出现；结合上下泳道连续性判断，不能一律取最亮像素。
4. 用远近两根泳道绳的中心线描述边界，按从左到右的分段折线记录。
   黄带半宽是绘图样式，不是实际绳宽。别把水下漂移的倒影当边界。
5. 以文件名最后一个数字段÷100 解释米数，仅适用于明确类似 `025/050/075` 的命名。
   顺序按数值米数升序，不按字符串排序；不要假设距离向画面右方增加。
   泳道宽默认 2.5m，标明是默认值；用户给出的现场宽度或距离表优先。
6. 每条记录 `high/medium/low` 置信度，低置信度写具体原因。
   被人遮住的主绳若无法判断，不得编造高置信度坐标；标明推断依据或请求必要补充。
   明显弯曲无法用直线合理表示时，报告限制并先改数据契约/渲染器，不能静默拉直。
7. 写出完整 JSON 到 `python/water_entry/visual_trials/<dataset_id>.json`。
   `dataset_id` 用 ASCII 字母、数字、下划线或连字符；不要覆盖其他已验收记录。
   新视觉结果标 `pending visual review`，不继承 0909 的验收状态或 reference_render。
8. 运行渲染器，打开合成图逐条比对；疑似错线回到原图重看，再改坐标重建。
   不靠平滑/等距排版消除真实差异。完成交付后才交给用户验收。

## 坐标记录格式（schema_version=1）

以下是**格式示例**，坐标仅演示字段，不能拿来标定新图。
`sha256` 必须填原图文件字节真实摘要，不是文件路径的摘要。

```json
{
  "schema_version": 1,
  "dataset_id": "new_session",
  "size": [1280, 720],
  "base_image": "capture-050.jpg",
  "status": "pending visual review",
  "method": "Assistant visually traced the physical cord in each source",
  "scope": "second lane counting away from the camera",
  "lane_index_from_camera": 2,
  "distance_interpretation": "filename final integer / 100 metres from pool wall",
  "lane_width_m": 2.5,
  "lane_width_status": "assumed; not independently measured",
  "far_boundary": [[0, 590], [900, 600]],
  "near_boundary": [[0, 625], [950, 635]],
  "boundary_half_width_px": [5, 6],
  "label_offsets_px": [18, 34],
  "lines": [
    {"file": "capture-025.jpg", "metres": 0.25, "samples": [[810, 605], [850, 620]], "confidence": "high", "sha256": "<64位真实摘要>"},
    {"file": "capture-050.jpg", "metres": 0.50, "samples": [[770, 605], [805, 620]], "confidence": "low", "note": "反光遮挡，需重点复核", "sha256": "<64位真实摘要>"}
  ]
}
```

- `size` 为全部原图的实际宽高，必须一致；`base_image` 必须是 `lines` 中的一张。
- 像素坐标原点左上，X 向右、Y 向下；采样点是原图像素，不是归一化 UV。
- `far_boundary` / `near_boundary` 各至少两个点，X 严格递增。
- `lines` 至少两条、米数严格递增、文件名无路径且不重复；每条恰好两个主绳采样点。
- 图外边界交点保留外推位置并在页面/报告中披露，不裁 UV 来伪造世界坐标。
- `accepted_on` / `reference_render` 仅在用户验收后记录。后者存已验收输出摘要和运行版本，
  不依赖旧图片存在。不得用这次新渲染的摘要覆盖旧摘要来制造“复现成功”。

```powershell
python -m python.water_entry.visual_trial --source "<原图目录>" --annotations "python/water_entry/visual_trials/<dataset_id>.json"
```

## 指标复核与一轮修正（每次必做）

渲染器会自动用 `python.water_entry.visual_metrics` 对每张原图运行局部指标，
输出 `review_metrics.json` 和 `review/<米数>.png`。不需要另装图像检测模型。
指标只读原图及当前两个采样点，不改坐标、不使用背景分离或跨帧融合。

1. 先渲染基准，保存本轮修改前的指标。逐条读取报告，优先查看用户点名的米数、
   low 置信度、`weak_image_support`、`nearby_stronger_trace`、`search_limit_reached`。
   用户提出疑点时，即使原记录为 high，也必须重看原图。
2. 指标在采样线段上取 41 个位置，比较中心灰度和法向两侧 3–4px 灰度，报告
   `contrast_gray`（中位灰度差）与 `support_fraction`（差值 >5 的比例）。
   在两个端点各 ±6px 法向范围内以 1px 步长搜索，报告候选点、偏移和对比度增益。
   对比度 <5 或支撑比例 <0.6 标弱支撑；增益 >5 且偏移 ≥2px 标邻近强线；
   搜索触边也必须复核。阈值是像素启发式，不是厘米精度或自动验收标准。
3. 打开每个疑点的核对图：左侧原始裁剪，右侧青线为原记录、品红为候选，
   标题给出原图裁剪起点和 6 倍缩放。再看完整原图，确认候选是目标泳道的
   同一根物理主绳。不能只挑指标最高者；倒影、浮标边缘同样可能高分。
   触边、遮挡或明显弯曲时，不无限扩大搜索，也不强行改成直线。
4. 看图支持修正时修改采样点，记录原点、新点、视觉理由、前后指标；
   不能通过移动边界、改米数或等距平滑来“修好”指标。
   不支持修正的疑点保留原点，在 `review_round.decisions` 中逐条记录具体原因。
   无法辨别者降为 low，明确留待用户复核，不能写“已修复”。
5. 原已验收 JSON 不覆盖。修正版写入
   `python/water_entry/visual_trials/revisions/<dataset_id>_review.json`，
   使用独立 dataset_id，状态 `pending visual review`，删除 `accepted_on` 和
   `reference_render`，添加 `review_round`（基准记录、方法、逐条决定和指标）。
   revisions 不参与自动指纹匹配，防止与基准重复命中；显式传 `--annotations` 渲染。
   同名修正版已存在时，先读取其来源和决定，新增编号保存本轮，不覆盖其他结果。
6. 用修正版再次运行同一入口，读取新的 `review_metrics.json`，比较修改项前后
   指标，打开修正后的核对图及合成图。指标退化或视觉仍错时撤回该修改并披露。
   一轮后仍不确定的点必须保留问题，不能循环优化直到凑够阈值。
   基准仍按历史摘要检查；修正版明确“不与历史摘要相同，待视觉验收”。
7. 在最终回复给出修正版验收页、改动米数、前后指标及未解决疑点。
   0909 的 1.25m 已被用户指出可能偏线，重启会话也必须纳入复核；
   `revisions/0909_review.json` 是首轮修正记录，不是新验收真值。

```powershell
python -m python.water_entry.visual_trial --source "<原图目录>" --annotations "python/water_entry/visual_trials/revisions/<dataset_id>_review.json"
```

## 交付目录

默认输出 `outputs/water_entry/calib/visual_trial_<dataset_id>/`，允许 `--output` 指定别处。
不放手写代码或提示词到 outputs。生成完整目录：

| 文件 | 必须满足的内容 |
| --- | --- |
| `overlay.png` | 原图同尺寸 RGBA，背景 alpha=0，只有黄带、红距离线、米数和标签短引线 |
| `overlay.white.png` | 同尺寸白底预览 |
| `overlay.composite.png` | 原位叠到 `base_image`，不裁剪、拉伸、透视矫正原图 |
| `overlay.svg` | 同尺寸/viewBox，可编辑的相同几何和文字；字体栅格化可能与 PNG 不同 |
| `surface.trial.fbx` | ASCII FBX 7.4，水面网格、米制世界坐标及 UV；无需材质或旧 FBX |
| `surface.json` | 同源顶点、UV、三角形、泳道身份、每条原图及采样/交点/置信度/摘要 |
| `annotations.json` | 本次输入坐标记录的完整副本 |
| `verification.json` | 输入指纹、运行版本、线数、顶点/三角形数、画外 UV、低置信度、复现摘要结果 |
| `review_metrics.json`、`review/` | 每条线的原图局部支撑指标、候选偏移及原图/描线核对图；不代表自动验收 |
| `sources/` | 所有参与标定的原图副本，名称不变，支持离线验收 |
| `index.html` | 下述离线交互页面 |

绘制规范：3 倍超采样再 LANCZOS 缩回原图；红色 `#ff0000` 距离线宽 2px，
黄色 `#ffff00` 两条带用记录中的半宽；米数两位小数加 `m`、12px 红字、白色 1px 描边。
米数横坐标对齐近端，纵坐标按 `label_offsets_px` 轮换，标签用 1px 红短引线连接。
0909 用 `[18,34]`。新图标签若出画/重叠，视觉检查后修改这些样式参数，不移动标定点。

FBX 规范：每个米数列两个顶点，先远侧 `(distance,0,0)`、后近侧 `(distance,lane_width,0)`；
X 离壁距离，Y 远侧到近侧，Z 向上，单位米。`u=pixel_x/W`，`v=1-pixel_y/H`。
相邻列两片三角形，N 条线得到 2N 顶点、2(N−1) 三角形。
本地坐标不冒充旧 FBX 的世界原点；标签引线/黄带不进入网格。
只允许 `python/fbx_tools/` 操作 FBX 格式，复用 `write_surface.py`。

## index.html 的固定要求

使用源码模板 `python/water_entry/visual_review.html`，不要每次自由设计页面。
若模板缺失，按以下契约恢复。HTML UTF-8，可双击 `file://` 打开，不启动服务、无 CDN、
无网络字体/外链脚本、无需 `fetch` 读取本地 JSON，数据内嵌并安全转义。

- 深色背景，标题显示数据集名和从镜头数第几条泳道。
- 显示原图尺寸、米数/泳道宽口径、验收状态、低置信度距离列表和画外交点数量。
  内容来自当前 JSON，不得写死 0909 的图片名、19 条、像素高度或疑点。
- 原图下拉框按米数升序，显示文件名、米数、置信度；上一张/下一张循环切换。
- “全部叠图”和“当前描线（青色）”独立开关；总叠图透明度滑条 0–1、步进 0.05、默认 0.8。
- 相机原图按原始纵横比展示，叠图与青线共享同一坐标系；不为填满页面变形。
- 青线只标当前源图的远近交点连线，两个采样点画空心青色圆圈。
- 当前帧旁显示置信度、具体复核原因；原图或叠图缺失时提示读取失败。
- 提供上述 PNG、SVG、FBX、网格 JSON、坐标记录和验证报告的相对链接。
- 切换到任意一帧后，原图、当前青线和备注必须同时更新；不能只换图片。

## 验证与完成条件

1. 渲染前检查全部输入指纹、尺寸、字段、米数顺序、边界方向和采样点有效性。
   指纹不符必须重新视觉取点，不能跳过校验强行用旧记录。
2. 验证 PNG 真透明、尺寸正确；交点落在选定的同一条泳道；红线不交叉。
3. 检查 `verification.json`：精确重建模式下 `reference_matches` 每项必须为 true。
   PNG 比较解码后 RGBA 像素摘要，SVG/FBX 比较文件摘要；页面因验收状态/模板更新
   不要求与试验初稿逐字节一致，但须满足上述固定交互契约。
4. 若渲染版本不同导致像素摘要不一致，先检查 Pillow/字体版本，保留已验收摘要。
   只能如实报告“不完全一致”，不能宣称仅凭提示词就能逐像素复现。
5. 有 ufbx 时独立读回 FBX，将顶点和 UV 与 `surface.json` 比较，容差 1e-10。
   新环境缺少读取器时补齐或明确说明未进行独立读回，不能把“已写出”当“已验证”。
6. 用浏览器实际检查验收页的切帧、两个开关、透明度和本地资源；无浏览器工具时
   至少检查 HTML/JS、链接和数据一致性，并如实记录未进行浏览器交互验证。
7. 完成后给用户简短中文说明：选中泳道、验收页/合成图/透明图/FBX 链接、疑点、
   是否复用了已验收坐标、哪些验证通过。用户已有视觉验收不等于有实测厘米精度。

## 删除输出、环境与可复现边界

可以删除整个 `outputs/`。保留原图目录以及这个 MD、`visual_trial.py`、
`visual_review.html`、`write_surface.py` 和 `visual_trials/*.json` 等源码即可重建。
这些是可复现配方，不是运行产物；无需保留旧设计师数据。
若连已验收 JSON 也删了，仍能按视觉步骤重做，但无法保证与本次坐标逐点相同。
纯提示词模式可追求相同质量和格式，精确复现由保存的坐标、模板和校验共同保证。

项目环境仍按 AGENTS.md 使用 Python 3.10；此渲染器本身只依赖 Pillow、NumPy。
本次已验收像素参考使用 Python 3.12.10、Pillow 12.3.0、NumPy 2.5.3，实际版本
以 `0909.json` 的 `reference_render` 为准。原机器可使用 `py -3.12 -m ...`；
不要为运行这个小工作流重装全部 C++/FBX SDK 或更改项目其他链路依赖。
移机要求精确像素时，用独立环境安装记录的版本；跨版本先运行摘要检查再作结论。

提示词组织参考：[OpenAI Prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering)。
本工作流的泳道、坐标和绘图口径来自本项目的实际验收。
