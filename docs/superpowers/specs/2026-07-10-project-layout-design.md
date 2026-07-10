# Swim FBX Demo 目录整理设计

日期：2026-07-10

## 目标

将当前根目录中混放的输入、代码、派生数据、图片、视频、日志和文档分开存放，同时修正 Python 导入和所有受影响的路径。整理不得删除或重新编码任何现有媒体产物。

4K 原始数据集保留在项目外，默认位置固定为：

```text
/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K
```

该目录现有六路 `20260629_172532_camN.mp4` 视频及 manifest、validation、sync map 三个元数据文件。项目只读取这些文件，不移动或修改它们。

## 目标目录

```text
swim_fbx_demo/
├── README.md
├── .venv/
├── inputs/
│   ├── models/
│   │   └── pool.fbx
│   └── textures/
│       ├── camera_1_composite.png
│       └── ...
├── outputs/
│   ├── data/
│   │   └── pool_mesh.json
│   ├── images/
│   │   ├── pool.png
│   │   ├── pool_grid.png
│   │   └── pool_grid_preview.png
│   ├── videos/
│   │   ├── pool.mp4
│   │   ├── pool_.mp4
│   │   ├── pool_4k_full.mp4
│   │   └── pool_4k_test10s.mp4
│   └── logs/
│       └── pool_4k_full.log
├── src/
│   ├── bake_uv.py
│   ├── extract_fbx.py
│   ├── fbx_common.py
│   └── render_pool.py
├── scripts/
│   └── run_4k.sh
└── docs/
    └── superpowers/specs/
```

`.venv` 保留在项目根目录，不移动或重建。根目录 `README.md` 是项目入口文档，因此不放进 `docs/`。

## 文件移动映射

| 当前路径 | 目标路径 |
| --- | --- |
| `FbxCommon.py` | `src/fbx_common.py` |
| `bake_uv.py` | `src/bake_uv.py` |
| `extract_fbx.py` | `src/extract_fbx.py` |
| `render_pool.py` | `src/render_pool.py` |
| `run.sh` | `scripts/run_4k.sh` |
| `pool.fbx` | `inputs/models/pool.fbx` |
| `textures/*.png` | `inputs/textures/*.png` |
| `pool_mesh.json` | `outputs/data/pool_mesh.json` |
| `pool.png`、`pool_grid.png`、`pool_grid_preview.png` | `outputs/images/` |
| 根目录全部 `.mp4` | `outputs/videos/` |
| `pool_4k_full.log` | `outputs/logs/pool_4k_full.log` |

现有历史图片、视频和日志只移动，不删除、不改名、不改内容。macOS 生成的 `.DS_Store` 不属于项目资产，整理时删除。

## 代码和路径规则

### Python 脚本

- `FbxCommon.py` 重命名为 `fbx_common.py`，`bake_uv.py` 和 `extract_fbx.py` 同步改为 `import fbx_common`。
- 脚本内定义项目根目录，例如由 `Path(__file__).resolve().parents[1]` 推导。
- 未显式传参时，默认模型为 `inputs/models/pool.fbx`，默认纹理目录为 `inputs/textures`，默认网格数据为 `outputs/data/pool_mesh.json`。
- `extract_fbx.py` 生成的 `source` 和 `texture` 元数据使用项目相对路径，不继续写入旧工程或本机的绝对路径；整理后重新生成 `pool_mesh.json`，三角形和 UV 数据保持同一来源。
- 用户显式提供的相对路径仍相对于调用命令时的当前目录解析；绝对路径保持原样。
- 写输出前创建所需父目录，避免因目标目录尚不存在而失败。
- 保持现有 FBX、UV、映射、合成和编码算法不变。

### 4K 运行脚本

- `scripts/run_4k.sh` 使用自身位置推导项目根目录，不依赖从哪个目录调用。
- 默认数据集目录为 `/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K`。
- 环境变量 `SWIMMING_DATASET_DIR` 可以覆盖默认数据集位置。
- 默认会话名保持 `20260629_172532`，视频顺序保持 `cam3 cam2 cam1 cam4 cam5 cam6`，与网格顺序一致。
- 缺省输出进入 `outputs/videos/pool_4k_test<秒数>s.mp4`；显式提供输出路径时使用该路径。
- Python 解释器优先使用项目根目录 `.venv/bin/python`，不存在时回退到 `python3`。

## 数据流

1. `bake_uv.py` 读取 FBX 和合成纹理，将 UV 扩展写入用户指定的新 FBX。
2. `extract_fbx.py` 读取 `inputs/models/pool.fbx`，默认生成 `outputs/data/pool_mesh.json`。
3. `render_pool.py` 使用网格 JSON 与 `inputs/textures` 生成静态图片，或使用六路视频生成拼接视频。
4. `run_4k.sh` 固化 4K 数据集、摄像机顺序、网格路径和默认输出路径，作为常用入口。

`outputs/data/pool_mesh.json` 是派生数据：它既是 FBX 提取步骤的输出，也是渲染步骤的输入，因此按来源归入 `outputs/data`，渲染器通过明确路径消费它。

## 错误处理

- 在开始重计算前检查模型、JSON、纹理目录、六路视频和 `ffmpeg` 是否存在。
- 纹理无法由 OpenCV 读取时，报告具体文件，而不是后续以空对象属性错误退出。
- 视频数量与网格数量不一致、视频无法打开、FBX 无法加载时继续保留现有的非零退出行为，并使错误信息包含路径。
- 不自动寻找或猜测其他数据集、会话或相机文件。

## README 范围

根目录 `README.md` 使用中文说明：

- 项目用途与处理链路；
- 完整目录结构及各目录职责；
- Python 3.10、Autodesk FBX Python SDK、NumPy、OpenCV 和 FFmpeg 依赖；
- 使用现有 `.venv` 的方式；
- UV 烘焙、FBX 提取、静态图、网格预览、4K 短片和全长视频命令；
- 默认外部数据集位置及 `SWIMMING_DATASET_DIR` 覆盖示例；
- 摄像机输入顺序和主要输出说明；
- 已知限制：只按最低帧率抽帧，不负责校正采集起始时间。

## 验证与验收

实施完成后执行以下检查：

1. 对照移动映射检查每个原文件都有且只有一个目标文件，并比对大文件字节数，确认没有删除或重新编码历史产物。
2. 使用项目 `.venv` 对 `src/` 执行 Python 编译检查。
3. 执行三个 Python 入口的 `--help` 或等价参数解析检查。
4. 验证 `fbx_common`、`fbx`、`cv2` 和 `numpy` 可导入。
5. 从项目根目录以外调用脚本，确认默认路径仍能正确解析。
6. 用较低 `ppm` 生成临时静态图片，验证 JSON、纹理和合成链路。
7. 使用外部 4K 数据集生成极短临时视频，验证六路视频顺序、OpenCV 读取和 FFmpeg 编码链路。
8. 临时验证产物不并入历史输出；不重新渲染 10 分钟全长结果。

验收标准是：根目录只保留入口文档、环境目录以及 `inputs`、`outputs`、`src`、`scripts`、`docs` 五个项目目录；README 中的命令可按文档运行；所有原有媒体产物仍存在且字节数不变；默认路径不依赖调用者当前目录。

## 范围外事项

- 不修改拼接算法、UV 参数、相机顺序或编码质量。
- 不移动或修改外部 4K 数据集。
- 不重建虚拟环境，不引入 Python 打包配置或新的命令行框架。
- 不删除历史产物，不重新渲染完整视频。
