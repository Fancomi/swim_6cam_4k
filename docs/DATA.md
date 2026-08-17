# 标定数据（inputs/）

`inputs/` **不在 git 里**，一个字节都没有——只有 `inputs/configs/` 的两份手写运行时
config 例外。2026-08-17 从全部 188 个提交里彻底抹除，`.git` 从 60MB 降到 1.8MB。

## 为什么不入库，也不上 LFS

这批数据是 225MB 的泳池实拍合成图与 FBX，其中 `pool 1.fbx` 单文件 61.5MB。三个理由：

- **git 存不下版本**。同一批相机换一份重建的 FBX 就是一条新线（`pool`→`pool2`），
  已经发生过 4 次（underwater2 的 FBX 改了四版）。每版都进库就是每版留一份 61MB 的
  副本，永久躺在历史里——25MB 的源码树驮着几百 MB 的图，且只会涨。
- **GitHub LFS 免费额度是 1GiB 存储 + 1GiB/月流量**。全传 225MB 后，**一次完整
  `git clone` 就吃 225MB 流量**（默认会拉全部 LFS 对象），一个月约 4 次 clone 用完，
  之后 clone 直接失败。加第二版就超存储。这个额度装不下这个项目。
- **图里有正在跳水/游泳的运动员**，仓库当前是公开的。LFS 对象一旦 push 到公开仓库
  就可能被缓存或索引，删了也不一定消失。

所以数据走**带外搬运**（网盘/移动硬盘），仓库只留代码、口径与这份清单。

## 目录结构

数据按**物理机位**分目录，按**代**分文件——两代的文件是并排放的，不是两个目录：

```
inputs/
├── configs/                    ← 唯一入库的部分（手写的 macos_*/windows_* 参考 config）
├── pool/
│   ├── models/pool.fbx         ← v1
│   ├── models/pool 1.fbx       ← v2（+ pool 1.fbm/ 贴图目录）
│   └── textures/               ← v1 的 6 张合成贴图
├── underwater/models/
│   ├── all.fbx    + all.fbm/   ← v1
│   └── 8.15.fbx   + 8.15.fbm/  ← v2
├── overhead/
│   ├── ' label_line.png'       ← 泳道示意图，两代共用（注意文件名有前导空格）
│   └── models/
│       ├── 002.fbx      + 002.fbm/      ← v1
│       └── '25 水面.fbx' + '25 水面.fbm/' ← v2
└── water_entry/
    ├── background.jpg          ← v1 底图（旧模型无全屏矩形）
    └── models/
        ├── 005.fbx, 006.fbx    ← v1（各一块 mesh）
        └── femto.fbx, gemini.fbx ← v2（各含全屏矩形+垂直+水面）
```

`*.fbm/` 是 FBX SDK 的配套贴图目录，必须与同名 `.fbx` 并排存在，否则读出来的 mesh
没有 `texture_basename`，`still` 会报找不到贴图。

## 场地与三类相机

50m × 26m 八道泳池，标定的是 **2 道**。三类机位：

| 机位 | 相机 | 位置与视角 | 覆盖 |
| --- | --- | --- | --- |
| 入水（water_entry） | 2 | 2 道侧面水上 | 起跳到入水那一瞬 |
| 水下（underwater） | 16 | 侧面水平置于水中 | 2 道 25m 全程 |
| 俯视（pool / overhead） | 6 | 两侧墙上斜 45° 向下，前中后各 3 | 6 台看满全池；其中 `overhead5`/`overhead6` 这两台看 2 道，单独成 overhead 线 |

**`pool` 与 `overhead` 是同一批 6 台相机的两种取法**：`pool` 用全部 6 台拼整池，
`overhead` 只取盯着 2 道的那 2 台拼一条泳道。所以 `pool 1.fbm/` 里同时有
`xlj_aux_zcam_1..4_merged.png` 和 `overhead5/6_merged.png`——后两张与
`25 水面.fbm/` 里的同名文件是同一批素材的不同导出。

## 两代的差异

一代是最初的标定，二代是**同一批相机重新建模/重新测量**后的产物。相机没换，片段没换，
换的只是 FBX 与它的贴图——所以每一代都是一条独立的线，不是一个新步骤。

| | v1 | v2 | 二代改了什么 |
| --- | --- | --- | --- |
| **pool** | `pool.fbx` 6 块 1040 三角 | `pool 1.fbx` 6 块 2756 三角 | 手工重建、网格加密约 2.6 倍；文件建成**转了 180°**（世界 Y 从 `[4.24,25.24]` 变 `[-25.24,-4.24]`），所以 profile 要 `neg_u`+`neg_v` 两个镜像一起开才落回旧坐标系。相机身份**按贴图像素认**，不按世界位置——两代的 6 台排布相反 |
| **underwater** | `all.fbx` 73 节点（16 块平面 + 杂物） | `8.15.fbx` 15 节点（干净） | 换标定物后重新测量。已迭代四版：整体去掉最下 0.25m → A16/A9 各短 0.5m → **去掉 A1**（相机从 16 台变 15 台）、贴图从 mask 合成图换成裸背景 → A10 的 UV 下移约 5px/3.8cm。v1 需要 `planes_only` 过滤杂物，v2 不需要 |
| **overhead** | `002.fbx` 2 块（120/204 三角） | `25 水面.fbx` 2 块（200/340 三角） | 2.5m 宽的泳道**多了一条中间拉线**（Y 从 2 行变 4 行：0/0.875/1.625/2.5m）；整体上移 11.47m（纯平移，下游不受影响）。渲染口径与 v1 逐字段相同，两代落在同一张 4255×515 画布上，可逐块比对 |
| **water_entry** | `005.fbx` + `006.fbx`，各一块 mesh，底图另给 `background.jpg` | `femto.fbx` + `gemini.fbx`，各 3 块 | 从「一个 FBX 一块 mesh」变成「一台相机一个 FBX，装全它的 mesh」：全屏矩形（纹理**就是**相机原图，不再需要外部底图）+ 垂直面 + 水面。femto 与 005/006 是同一台相机，位置略有不同 |

体量（决定你按代搬还是全搬）：

| | 文件数 | 大小 | 大头 |
| --- | --- | --- | --- |
| v1 | 50 | 50.7 MB | `all.fbx` 13.7MB、`002.fbx` 6.8MB |
| v2 | 35 | 174.8 MB | **`pool 1.fbx` 61.5MB**、`pool 1.fbm/` 6 张 4K 共 61MB、`25 水面.fbx` 17.4MB |
| 合计 | 84 | 225.5 MB | |

`overhead/ label_line.png` 两代共用，两边都算了一次，所以分代之和比合计多 1 个文件。

## 搬运与验收

数据不在 git，所以「我这份对不对」不再由 git 回答——一张缺失、截断或来自错误版本的
贴图，会在很久之后表现为拼接缝错位，症状伪装成「UV 标歪了」。所以搬完请校验。

```bash
# 需要哪代就搬哪代的目录（见上面的目录结构），放到仓库的 inputs/ 下，然后：
./scripts/check_inputs.sh          # 校验两代
./scripts/check_inputs.sh v2       # 只校验二代（只搬了二代时用）
```

校验依据是 `docs/data-manifest.tsv`：每行 `gen / line / sha256 / bytes / 路径`。
**路径不是手写的，是从 `python/stitch/profiles.py` 与
`python/fbx_overlay/profiles.py` 导出的**——加一条线不用改校验脚本，重新生成清单即可：

```bash
./scripts/check_inputs.sh --write   # 用当前 inputs/ 重写 docs/data-manifest.tsv
```

只搬一代时另一代必然报缺失，那是正常的：用 `v1` / `v2` 参数把范围缩到你实际搬的那代。

## 最少要搬多少

按你要跑的链路取，不必全搬：

| 想跑 | 需要 |
| --- | --- |
| `run_stitch.sh pool …` | `pool/models/pool.fbx` + `pool/textures/` |
| `run_stitch.sh overhead2 …` | `overhead/models/25 水面.fbx` + `.fbm/` + ` label_line.png` |
| `run_fbx_overlay.sh` | `water_entry/models/` 的 4 个 FBX + 各自 `.fbm/` + `background.jpg` |
| C++ 编译任何 target | 只需 `outputs/pool/mesh.json`，而它由 `run_stitch.sh pool extract` 从 `pool.fbx` 生成——所以 **pool 一代是编译的前置** |

最后一条容易踩：`outputs/pool/mesh.json` 是 CMake 的硬依赖，缺了任何 C++ target 都编
不过，而它需要 `pool.fbx`。只想编 C++ 也得先搬 pool 的 v1。

