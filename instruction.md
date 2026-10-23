# 三段热杆拆检后的测点回填

复材热工组刚拆开三段拼装试验杆，发现八支工艺热电偶的安装卡仍在，但通道标签与孔位记录对不上。杆件随后重新升温，表面温度、三支固定基准 RTD 和各通道的温度历程已经采回。现在要把测点对应关系、三段导热系数与两端换热条件一起交回工艺组。若只按通道顺序铺一排位置，材料分段的影响会被抹掉。

现有入口是 `/app/resolve_rod.py`，调度会为每个工况启动一个子进程（process），下面这条命令就是它的运行方式。`--job` 和 `--state-dir` 各出现一次，顺序不限，其他参数一律不接受。`--state-dir` 是已存在的真实目录，报告路径由工况文件给出。

```bash
python3 /app/resolve_rod.py --job <工况文件> --state-dir <状态目录>
```

`/app/data/` 里放着三条已脱敏的复核工况，文件名为 `campaign_a_job.json`、`campaign_b_job.json` 和 `campaign_c_job.json`。它们分别包含不同的材料分段位置、容量、初始温度、坏通道组合和实际输出路径。`DATA_SOURCE.txt` 记录了这些记录的试验模型与固定随机种子。

试验准备记录里，三段材料通过薄层压合连接，杆件外表面包覆同一种保温套。这里的容量是包含保温修正后的等效体积热容量，导热系数描述杆轴方向的传热。表面探头用于观察左端附近的换热强度，三支固定 RTD 由校准工装压紧在已知深度，给其余工艺通道提供参照。

工艺热电偶的响应时间明显快于整根杆的升温时间，采样记录因此可以按同一瞬时温度处理。各通道的安装深度改变温度读数对应的轴向位置，不改变材料属性。试验台上的连接器复插过后，少数通道会出现整段冻结或固定偏置，这也是现场记录里需要单独辨认的测量问题。

现场采样使用秒作为时间单位，摄氏度作为温度单位。通道标签来自端子排，安装卡上的孔号则来自拆检图，两套编号在返修时被分开归档。左右两端的夹具温度由恒温箱记录，杆件入箱前已经静置到同一温度。复核人员现在拿到的是端子排曲线和拆检图，而不是逐支探头的配对表。

## 热过程口径

杆长方向记为 \(x\)，范围为 \(0\) 到 `length_m`。三段首尾相接，容量与导热系数在每段内为常数，段界处温度连续、热流连续。初始温度全场均匀，两端采用 Robin 换热：

\[
C(x)\frac{\partial T}{\partial t}
=\frac{\partial}{\partial x}\left(k(x)\frac{\partial T}{\partial x}\right),
\]

\[
k(0)\frac{\partial T}{\partial x}\bigg|_{x=0}
=h_L\left(T(0,t)-T_\infty\right),
\qquad
-k(L)\frac{\partial T}{\partial x}\bigg|_{x=L}
=h_R\left(T(L,t)-T_\infty\right).
\]

容量、段界、初始温度和 `ambient_c` 已由材料批次记录给出；三段 `k`、`h_L` 和 `h_R` 要从温度历程反演。`surface_temperatures_c` 是左端面上的独立表面探头，三支 `reference_channels` 是已校准孔位，位置直接给出。

八支工艺通道的 `channel_id` 与位置没有对应关系，输入数组顺序也不能作为位置顺序。位置在杆内连续变化，不能只在几个等距孔位中挑选。坏通道数量不超过 `max_outliers`；坏通道可能整段冻结、从第一个采样起带异常偏置，或有相同的冻结曲线。保留参与认证的通道必须共同支持同一条热历程，正好去掉数据里存在的坏通道。

反演结果要满足下列口径。位置误差按每个通道与其真实安装点的欧氏距离计算；方向相关性按“位置递增、观测冷却强度递减”的秩相关计算，不能用位置误差代替方向口径。三段导热系数按各自真值计算相对误差，左端换热系数也按真值计算相对误差；右端接近绝热，按绝对差计算。

| 判定量 | 通过口径 |
| --- | --- |
| 单通道位置 | 与真实安装点的绝对距离不超过 `controls.map_tolerance_m` |
| 三段导热系数 | 每段相对误差不超过 `controls.parameter_rel_tolerance` |
| 左端换热系数 | 相对误差不超过 `controls.parameter_rel_tolerance` |
| 右端换热系数 | 与真值的绝对差不超过 `0.12 W/(m^2 K)` |
| 方向相关性 | 不大于 `-0.95` |
| 位置诊断 | `mapping_diagnostics.distance_rms_m` 不超过 `controls.map_tolerance_m` |
| 拟合残差 | `consensus.rms_c` 不超过 `controls.rms_tolerance_c` |
| 稳定性 | `timestep.max_fourier` 不超过 `controls.max_fourier`，且 `timestep.dt_s` 大于零 |
| 邻位唯一性 | `certificate.unique_margin` 不小于 `0.03` 摄氏度 |

同一工况在改变空间节点数后仍要落在上面的参数与位置口径内。`controls.max_seconds` 是单次工况从开始校验到发布完成的公开上限，正常工况必须在时限内完成，并给出真实的实际步长与最大网格 Fourier 数。

## 工况文件

工况文件是 UTF-8 JSON 对象。对象中同名键重复时，无论位于哪一层都拒收。顶层键恰好是 `schema_version`、`job_id`、`output`、`state_key`、`rod`、`observations`、`controls`，缺键和多键都拒收。

下面的文本列出各层内容。

```text
顶层 = schema_version, job_id, output, state_key, rod, observations, controls
schema_version = "rod-inverse/v1"
job_id = 匹配 JOB-[A-Z0-9]{4}-[A-Z0-9]{4} 的字符串
output = 绝对 POSIX 路径字符串
state_key = 匹配 [a-z0-9][a-z0-9-]{2,31} 的字符串

rod = length_m, segments, initial_temperature_c, ambient_c, boundary_bounds
rod.segments[] = x0_m, x1_m, capacity_j_m3k, conductivity_bounds_w_mk
rod.boundary_bounds = left_h_w_m2k, right_h_w_m2k

observations = times_s, surface_temperatures_c, reference_channels, channels, max_outliers
observations.reference_channels[] = channel_id, position_m, temperatures_c
observations.channels[] = channel_id, temperatures_c

controls = max_seconds, fd_nodes, max_fourier, rms_tolerance_c, map_tolerance_m, parameter_rel_tolerance

segments: 3 项；conductivity_bounds_w_mk: 2 个数字
reference_channels: 3 项；RTD 标签匹配 RTD-[A-Z][0-9]
channels: 5 到 12 项；TC 标签匹配 TC-[A-Z][0-9]；标签不重复
温度数组长度等于 times_s 长度；reference 位置递增且在杆内

length_m: 0.1 到 3.0 米
容量: 正值
导热系数 bounds: 正值且下界小于上界
initial_temperature_c 与 ambient_c: 相差至少 10
温度读数: -80 到 500 摄氏度
times_s: 8 到 64 项；第一项 0；后续严格递增；末项不超过 7200 秒
max_outliers: 0 到 3 的整数
controls.max_seconds: 1 到 30 秒
controls.fd_nodes: 48 到 128 的整数
controls.max_fourier: 0.30 到 1.20
controls.rms_tolerance_c: 0.05 到 1.0
controls.map_tolerance_m: 0.002 到 0.05
controls.parameter_rel_tolerance: 0.05 到 0.40

JSON 数字不含布尔、字符串、数组、对象、null、NaN 和无穷
output 不含反斜杠、空字节、"." 或 ".." 路径段
output 父目录存在且为真实目录
已有 output 应为普通文件；目录、链接和断链都不接受
--state-dir 应指向真实目录
```

## 交付报告

认证成功后写到工况文件的 `output` 路径。报告是一行紧凑 UTF-8 JSON：分隔符后不加空格，末尾恰好一个 LF。顶层键按下面顺序出现，键集恰好是这十一项：

```text
schema_version, job_id, status, sensor_map, conductivity_w_mk, boundaries,
timestep, consensus, mapping_diagnostics, certificate, ledger
```

报告版本值固定为 `rod-inverse-report/v1`。`job_id` 原样复制。`status` 固定为 `certified`。

```text
sensor_map[] = channel_id, x_m, support
  x_m 递增；support 为 0 到 1
conductivity_w_mk[] = segment, x0_m, x1_m, k_w_mk
  3 项；segment 为 0, 1, 2；边界复制输入
boundaries = left_h_w_m2k, right_h_w_m2k, ambient_c
timestep = nodes, dt_s, max_fourier
consensus = good_channels, outlier_channels, rms_c, support_count
  good_channels 和 outlier_channels 使用字典序
mapping_diagnostics = distance_rms_m, direction_correlation, rank_margin
certificate = accepted, support_count, unique_margin, parameter_sha256
ledger = state_key, event_sha256, sequence
```

报告浮点数字最多保留六位小数，半位采用远离零的进位。JSON 中写数字，不写数字字符串，不出现负零、NaN 或无穷。整数保持整数。

`parameter_sha256` 的输入是紧凑 UTF-8 JSON，不含换行，键顺序依次为 `sensor_map`、`conductivity_w_mk`、`boundaries`，三个值原样使用报告中的六位小数结果。对该字节串计算 SHA-256，写成 64 位小写十六进制。

## 求解账本

账本放在状态目录，文件名为 `<state_key>.jsonl`。每个成功认证事件占一行，行内是紧凑 JSON，末尾带 LF，键顺序为 `event_sha256`、`job_sha256`、`report_sha256`、`sequence`。四个值中前三个是 64 位小写 SHA-256，`sequence` 是从 1 开始连续递增的整数。

`job_sha256` 按工况文件原始字节计算。`report_sha256` 按最终报告文件字节计算。`event_sha256` 按“工况文件原始字节后接 `parameter_sha256` 的 32 字节原值”计算，不使用十六进制文本。同一事件已经在账本中时，账本一个字节都不改，重复运行的报告字节也保持一致。

不同事件按首次成功发布的先后追加。已有行不改写。账本损坏时按失败处理。

## 失败边界

校验、拟合、发布或账本处理失败时，退出码为 2。标准输出保持为空。标准错误只写一行：

```text
ERROR <CODE>: <message>
```

`<message>` 不写 traceback。其余错误文本保持为空。

```text
公开代码：
USAGE, DUPLICATE_KEY, JSON, UTF8, SCHEMA, TYPE, PATH, JOB_PATH, JOB_READ,
STATE_DIR, OUTPUT_PARENT, OUTPUT_TYPE, SCHEMA_VERSION, JOB_ID, STATE_KEY,
CHANNELS, CHANNEL_ID, TRACE_LENGTH, TRACE_RANGE, TIMES, SURFACE_LENGTH,
REFERENCE_CHANNELS, REFERENCE_ID, REFERENCE_POSITION, LENGTH,
TEMPERATURE_RANGE, BOUNDARY_BOUNDS, SEGMENTS, SEGMENT_EDGE, CAPACITY,
CONDUCTIVITY_BOUNDS, OUTLIER_BUDGET, FD_NODES, CONTROL_RANGE, CONSENSUS,
FIT_TOLERANCE, TIME_LIMIT, LEDGER_TYPE, LEDGER_CORRUPT, PUBLISH

标准错误逐字前缀：
ERROR USAGE:
ERROR DUPLICATE_KEY:
ERROR JSON:
ERROR UTF8:
ERROR SCHEMA:
ERROR TYPE:
ERROR PATH:
ERROR SCHEMA_VERSION:
ERROR TIMES:
ERROR CHANNELS:
ERROR REFERENCE_POSITION:
ERROR CONTROL_RANGE:
ERROR PUBLISH:
```

校验失败不能改报告或账本。输入文件保持原字节。若新报告已经替换而账本替换随后失败，必须恢复报告旧字节，账本仍保持旧字节。成功和失败后都不要残留 `.tmp`、备份或半成品。一次失败之后，另一工况仍应能够完成。

一次认证通过后，调度端不会从 stdout 或 stderr 收到任何字符。相同工况连续运行两次，报告字节完全相同，账本也不增加重复行。
