# Swim FBX Demo 目录整理设计

日期：2026-07-24
状态：已获用户批准，待实施

## 目标

按职责整理仓库目录，明确源码、运行输入、构建产物、运行产物、测试和文档的边界。

本次整理遵循以下原则：

1. 生成物不进入 Git。已经被 Git 跟踪的生成物只取消跟踪，保留本地文件，不删除、不重新编码、不改内容。
2. 运行输入统一位于 `inputs/`；构建工具链和源码不属于输入。
3. 运行结果统一位于 `outputs/`；可重建的构建缓存统一位于 `build/`。
4. Python 工具按包职责归入 `python/`；不保留顶层 `annotation-preview/`、`assets/`、`benchmarks/` 等与源码职责冲突的目录。
5. 路径迁移采用直接切换策略：全仓更新引用并删除旧路径，不保留兼容目录、软链接或双重入口。
6. 不改变媒体、模型、纹理和日志内容；只移动、改路径或取消 Git 跟踪。

项目外的 4K 原始视频数据集继续保留在：

```text
/Users/penghaotian/Downloads/DATAS/SWIMMING/20260629-4K
```

项目只通过配置或环境变量读取该数据集，不移动或修改项目外文件。

## 目标目录

```text
swim_fbx_demo/
├── README.md
├── CMakeLists.txt
├── cmake/                         # CMake 构建逻辑
├── cpp/                           # C++ 生产源码
├── python/                        # Python 生产包
│   ├── assets/                    # FBX/资产处理代码，不是静态资产
│   ├── annotation_preview/       # annotation preview Python 工具
│   │   └── dot_labeler/           # annotation preview Web 工具
│   ├── underwater/
│   └── validation/
├── scripts/                       # 用户可调用的运行脚本
├── inputs/                        # 受控运行输入
│   ├── configs/                   # 运行配置
│   ├── pool/
│   │   ├── models/
│   │   └── textures/
│   └── underwater/
│       └── models/
├── outputs/                       # 全部为运行结果，整体忽略
│   ├── annotation_preview/
│   ├── benchmarks/
│   ├── data/
│   ├── images/
│   ├── keypoint_preview/
│   ├── logs/
│   ├── underwater/
│   └── videos/
├── build/                         # 可重建构建目录，整体忽略
│   └── assets/generated/
├── tests/                         # 统一测试入口
│   ├── cpp/
│   ├── fixtures/
│   └── python/
└── docs/
    └── superpowers/specs/
```

以下目录属于本机或工具状态，不纳入项目结构设计：`.git/`、`.venv/`、`.worktrees/`、`.claude/` 和 `.pytest_cache/`。它们保持忽略，不参与迁移。

## 迁移映射

### 源码

| 当前路径 | 目标路径 | 操作 |
| --- | --- | --- |
| `annotation-preview/*.py` | `python/annotation_preview/*.py` | `git mv` |
| `annotation-preview/dot_labeler/` | `python/annotation_preview/dot_labeler/` | `git mv` |
| `cpp/tests/*.cpp`、`cpp/tests/*.mm` | `tests/cpp/` | `git mv`，同步更新 CMake |
| `cpp/tests/fixtures/` | `tests/fixtures/cpp/` | `git mv`，同步更新 CMake |
| `python/tests/*.py` | `tests/python/` | `git mv`，同步更新测试命令与导入 |
| `python/tests/fixtures/` | `tests/fixtures/python/` | `git mv`，同步更新测试引用 |

`python/assets/` 保持为 Python 代码包。它与静态输入资产无关，不移动到 `inputs/`。

### 运行输入

| 当前路径 | 目标路径 | 操作 |
| --- | --- | --- |
| `configs/macos_20260629.conf` | `inputs/configs/macos_20260629.conf` | `git mv`，清理绝对路径 |
| `inputs/models/pool.fbx` | `inputs/pool/models/pool.fbx` | `git mv` |
| `inputs/textures/*.png` | `inputs/pool/textures/*.png` | `git mv` |
| `inputs/models/01d.fbx` 及 `.fbm/` | `inputs/underwater/models/` | 移动本地输入，按大文件策略忽略 |
| `inputs/models/1-5.fbx` 及 `.fbm/` | `inputs/underwater/models/` | 移动本地输入，按大文件策略忽略 |
| `inputs/models/1(2).fbx` 及 `.fbm/` | `inputs/underwater/models/` | 移动本地输入，按大文件策略忽略 |
| `inputs/models/all.fbx` | `inputs/underwater/models/` | 移动本地输入，按大文件策略忽略 |

大体积或用户本地输入保留在磁盘，但不因本次整理自动加入 Git。是否纳入版本控制由后续单独决定。

### 运行产物和构建产物

| 当前路径 | 目标路径 | 操作 |
| --- | --- | --- |
| `assets/generated/pool_4k.swasset` | `build/assets/generated/pool_4k.swasset` | 移动本地文件，取消跟踪并忽略 |
| `benchmarks/manual.jsonl` | `outputs/benchmarks/manual.jsonl` | 移动本地文件，取消跟踪并忽略 |
| `benchmarks/runs/` | `outputs/benchmarks/runs/` | 移动本地目录，取消跟踪并忽略 |
| `annotation-preview/detections.csv` | `outputs/annotation_preview/detections.csv` | 移动本地文件，取消跟踪并忽略 |
| `outputs/**` | `outputs/**` | 保留分类路径，取消所有生成文件的 Git 跟踪并整体忽略 |
| `.superpowers/` 报告 | `.superpowers/` | 保留本地文件，取消已跟踪报告的 Git 跟踪并忽略 |

`outputs/` 内现有的 JSON、图片、视频、日志、keypoint preview、水下渲染结果均属于生成物，不迁入 `inputs/` 或 `docs/`。

## 引用更新

迁移时必须全仓搜索并更新下列引用：

- `CMakeLists.txt`、`cmake/` 中的 fixture、生成资产和测试路径；
- `scripts/run_metal.sh`、`scripts/run_python.sh`；
- `python/assets/`、`python/underwater/`、`python/validation/` 和 `python/annotation_preview/`；
- C++、Python 测试及测试发现命令；
- README、设计文档和运行说明；
- `.gitignore`、构建脚本和配置文件。

配置文件中的本机绝对路径改为仓库相对路径、环境变量或明确的本地覆盖项。annotation preview 中依赖外部数据集的路径统一从配置或命令行参数进入，不再硬编码互相矛盾的项目内路径。

迁移完成后，全仓搜索不得留下旧目录路径：

```text
annotation-preview/
assets/generated/
benchmarks/
configs/
inputs/models/
inputs/textures/
cpp/tests/
python/tests/
```

## Git 和忽略策略

新增或调整忽略规则，覆盖：

```text
build/
outputs/
.superpowers/
.pytest_cache/
inputs/pool/models/*.fbx
inputs/underwater/models/
```

受控的小型输入和源码继续跟踪；运行输出、构建缓存和本地大模型只保留在工作区。迁移提交不得包含 FBX、纹理伴随目录、图片、视频、JSON、CSV、日志或 `.swasset` 生成物。

## 验收标准

1. `git ls-files` 不再包含 `outputs/`、`assets/generated/`、`benchmarks/`、`.superpowers/` 下的生成文件。
2. 本地产物仍存在，且 `git status --short` 不再显示这些产物。
3. 顶层不再存在 `annotation-preview/`、`assets/`、`benchmarks/`；运行配置不再位于顶层 `configs/`。
4. 全仓搜索不再出现旧路径引用，且不存在失效的相对路径。
5. CMake configure、build 和 `ctest` 通过。
6. Python 测试通过。
7. `run_python`、`run_metal`、FBX 提取/渲染和 annotation preview 工具能够通过新路径运行；不可用的外部数据集场景至少能给出清晰错误。
8. `git diff --check` 通过，迁移提交不包含不相关的用户本地产物。

## 非目标

- 不重新编码、压缩或清洗任何媒体和模型。
- 不修改项目外 4K 视频数据集。
- 不重构算法、渲染逻辑或 C++/Python API。
- 不为旧路径保留软链接、包装脚本或兼容层。
