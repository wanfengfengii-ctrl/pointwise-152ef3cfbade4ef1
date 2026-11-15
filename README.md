# tiffwindow —— 旧 TIFF 高程影像的按窗口原始整数读取

采集端需要在不解码整幅影像的前提下，从历史高程 TIFF 中取出任意窗口的
原始整数样本。仓库内只有 Python 标准库实现，没有第三方依赖，也不调用
libtiff/PIL。

```python
from tiffwindow import read

rows = read("elevation.tif", x=120, y=80, w=21, h=17)
# rows 为 h 行 × w 列的整数二维列表；不做缩放、反色或 colour map。
```

窗口原点在左上角（Orientation=1）。`w`、`h` 必须为正，窗口必须完整落在
图内；不支持的 TIFF 变体、非法窗口、被读取条带损坏（LZW 流非法、码不在
字典中、缺 CLEAR/EOI、字节数不符、字典溢出、条带越界等）一律抛
`ValueError`，不会返回半成品。

## 支持范围

| 项目 | 取值 |
| --- | --- |
| TIFF 版本 | Classic TIFF（magic = 42，单页）；BigTIFF 拒绝 |
| 字节序 | `II`（little endian）或 `MM`（big endian） |
| 存储方式 | 条带（strips）；带 TileWidth/TileLength 的分块 TIFF 拒绝 |
| 通道 | 单通道灰度（SamplesPerPixel=1，PlanarConfiguration=1，无 ExtraSamples） |
| 位宽 | 无符号 8 位或 16 位（SampleFormat=1，BitsPerSample=8/16） |
| 方向 | Orientation=1 |
| 压缩 | Compression=5（TIFF LZW） |
| 预测 | Predictor=1 或 2（水平差分） |
| 填充 | FillOrder=1 |

`PhotometricInterpretation` 接受 0（WhiteIsZero）与 1（BlackIsZero），
因为两者只影响显示，不改变存储样本值。

## IFD 解析

- 按 TIFF 6.0 规则解析 IFD：字段值总长 ≤ 4 字节时内联于条目，否则第
  9–12 字节是文件内偏移；`StripOffsets`/`StripByteCounts` 每个元素同样
  按 SHORT/LONG 与内联/偏移规则逐项读取。
- 只保留读取所需的标签，其余标签流式跳过，畸形 IFD 不会触发大分配。
- 拒绝重复标签、越界偏移、条带表条数与 `RowsPerStrip` 推算出的条带数
  不一致、多页（下一个 IFD 偏移非 0）、SubIFD 与 ExtraSamples。
- `RowsPerStrip` 缺省时按 TIFF 默认值视为整幅一条带；末条带行数按
  `ImageLength` 截断。

## LZW 解码规则

- 码流为 MSB-first 打包，初始 9 位。**遵守 TIFF 的 early-change 规则**：
  当解码器即将建立的下一个自由码达到 511/1023/2047 时就把码宽加一，
  而不是等真正用到该码。验证方式见下文（错误策略的码流必须被拒绝）。
- 每个条带独立重置：读到 CLEAR（256）即清空字典、码宽回到 9 位、丢弃前
  一码；连续 CLEAR 与条带起始的重复 CLEAR 都接受。
- 支持字典自引用（KwKwK）：当前码等于“即将建立的码”时，输出
  `previous + previous[0]`。
- 字典满（4096 项）而未见 CLEAR 视为损坏；读到不在字典中的码、CLEAR 前
  出现数据码、缺 EOI、压缩流提前结束、解码字节数与条带应有字节数不符，
  均抛 `ValueError`。
- 末尾 EOI 的位宽按编码器语义推进（读最后一个数据码时解码器已建立一个
  新表项），因此与 libtiff 产出的码流可以逐字节对齐。

## 水平差分回绕

Predictor=2 时按样本位宽取模回绕：

- 8 位：`v[i] = (raw[i] + v[i-1]) & 0xFF`
- 16 位：`v[i] = (raw[i] + v[i-1]) & 0xFFFF`

每行从 `x=0` 起算（差分依赖左侧全部样本，不能只从窗口左边界开始），
只把窗口内的结果写入返回列表。

## 内存与 I/O 约束

- 只打开并读取与窗口相交的条带；窗口之外的条带载荷既不读取也不校验
  （文件以无缓冲方式打开，避免 Python 缓冲预读邻接字节）。
- 解码器边解边交给“按行切分”的 sink：只保留当前一行（影像宽度×样本
  字节）和 LZW 字典（4096 项的 prefix/suffix 两个整型数组），不缓存整条
  解压数据。压缩输入按 4 KiB 分块读取。
- 额外内存为 O(影像宽度 + 4096)，与影像高度、条带大小、窗口大小无关。
  `verify` 中实测：2000×1200 影像上取 3×3 窗口，峰值分配约 29 KiB，
  与 256×256 影像上同尺寸窗口（约 23 KiB）基本持平。
- 条带必须完整解码并校验（包括窗口以下的行走查字节总数），因此被读取
  条带内的损坏一定暴露；不相交条带损坏不影响本窗口，但若窗口跨到该条带
  则整体失败。

## 文件

| 文件 | 作用 |
| --- | --- |
| `tiffwindow.py` | 读取器，唯一公开接口 `tiffwindow.read(path, x, y, w, h)` |
| `tiffgen.py` | 独立的 TIFF/LZW 写入器，用于生成像素已知的测试影像（不 import 读取器） |
| `verify.py` | 业务核验（17 组、数百条断言），`python3 -I verify.py verify` |
| `demo.py` | 真实提取演示，`python3 -I demo.py`（也可对任意文件指定窗口） |
| `.pointwise-runtime.json` | 运行时清单：setup / verify / demo 的实际 argv |

`tiffgen.py` 的编码器是独立实现，既用来从源数组生成影像（从而无需通过
任何解码器就能知道期望像素），也在 `width_policy="late"` 时故意生成
“错误码宽”码流，证明读取器确实执行 TIFF 的 early-change 规则而不是碰巧
能解。

## 运行

```bash
# 环境自检（字节编译 + 隔离模式导入）
python3 -I -m py_compile tiffwindow.py tiffgen.py verify.py demo.py

# 业务核验：全部通过则退出码 0，最后一行 VERIFY_OK
python3 -I verify.py verify

# 真实提取演示：生成地形影像、打印窗口整数矩阵并与源数组逐像素对照
python3 -I demo.py

# 对已有影像提取指定窗口
python3 -I demo.py /path/to/elevation.tif 120 80 21 17
```

`.pointwise-runtime.json` 中 `setup`/`verify`/`demo` 即上述命令。

## 核验覆盖

`verify.py` 用独立生成器造图、用源数组算期望值，逐像素比对：

- 大小端 × Predictor 1/2 × 条带高度 1/7/50/全图（16 位与 8 位）；
- 码宽 9→10→11→12 的 early-change，包含恰好触发与不触发的边界；
  错误（late）码宽码流必须被拒绝；
- 显式清表：固定间隔 CLEAR、连续重复 CLEAR、字典满自动 CLEAR；
- 字典自引用（长游程触发大量 KwKwK）；
- 水平差分跨 0/最大值回绕（8 位与 16 位、两种字节序）；
- 跨条带窗口、单行/单列最小窗口、全图窗口、末条带矮条带；
- IFD 内联值（SHORT/LONG 的 ImageWidth、单条带内联的 StripOffsets）、
  偏移值（多值条带表、16 位 BitsPerSample）、SHORT 型条带偏移/字节数、
  缺省 RowsPerStrip；
- 拒绝路径：非 LZW、分块、Orientation≠1、多通道/多平面、ExtraSamples、
  Predictor≠1/2、12 位、有符号样本、FillOrder≠1、RGB 光度、SubIFD、
  多页、BigTIFF、非 TIFF、文件不存在、十种非法窗口、非整数坐标；
- 损坏条带：乱码码流、求码不在字典、缺 CLEAR、缺 EOI、字节数截短、
  条带越界、字典满不清理；窗口跨损坏条带时整体失败，邻近条带损坏时
  干净窗口仍可读；
- 读放大：插桩记录实际读取区间，断言窗口外条带一个字节都没读；
- 内存：tracemalloc 峰值在 O(宽度) 量级，且不随影像尺寸增长；
- 若系统有 ImageMagick/libtiff，额外用 libtiff 解码同一批夹具做交叉验证
  （无则跳过，不影响标准库核验）。

## 演示覆盖

`demo.py` 生成 480×360 的 16 位地形影像（MM、Predictor=2、23 行/条带），
打印跨条带窗口的整数矩阵并逐像素对照；随后依次演示 II/MM 两种字节序、
Predictor 1/2、8 位与 16 位、9→12 码宽与 CLEAR 频繁的条带、差分回绕、
以及 libtiff 独立编码的同一影像（若可用）。最后演示非法窗口被拒绝。

## 已知限制

只支持题目给定的子集：单页 Classic TIFF、无序外数据、条带存储、单通道
无符号 8/16 位、LZW + Predictor 1/2。其余情况直接 `ValueError`，不做
猜测或降级处理。
