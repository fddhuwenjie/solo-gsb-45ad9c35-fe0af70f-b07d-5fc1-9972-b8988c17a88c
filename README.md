# 温室分区轮灌编排台

基于 **Python Flask + SQLite + 原生 HTML/CSS/JavaScript** 的温室管理工具，包含七个页面：

1. **温室分区轮灌编排台**（`/`）：以 **15 分钟为最小粒度**，在总流量、阀门并发上限、允许灌溉时段和停水时段四类约束下自动生成一天的轮灌计划，并在时间轴上直观标出冲突原因；支持鼠标拖拽与表单两种方式微调单个时段，即时校验，可保存到数据库并导出 JSON。计划保存采用**防覆盖修订（乐观并发）**：页面加载时取得修订号，自动编排 / 拖动 / 表单保存均携带该基线，服务端仅在基线仍为当前修订时原子写入并递增；两个窗口基于同一版本编辑时先提交者成功、后提交者收到 409 且不产生部分更新，页面保留本地草稿并按分区列出**仅本地 / 仅服务器 / 双方都改**的差异，逐项选择后再基于最新修订提交；阀门流量、分区约束、停水时段或总流量变更同样使旧基线失效。
2. **灌溉水质连续监测**（`/water`）：按水源连续录入带时间戳的 **pH / EC / 浊度 / 硝酸盐** 采样，为每个水源配置各指标正常区间，按「连续两次越界判超标、连续两次回归判恢复」的规则重算状态，支持按水源/日期筛选、最近采样趋势图与 JSON 导出；**每个阀门可绑定一个水源**，并按计划日期提供「**用水回溯**」——把现有 15 分钟灌溉时段与水源半开状态区间求交，采样时刻起采用新状态、当日首条采样前沿用最近历史状态（无历史样本标证据不足），超标 / 观察区间分别计为风险 / 待复核水量，列出分区、触发指标与状态来源采样，支持分区筛选与 JSON 导出。
3. **病害接触追踪**（`/disease`）：复用现有温室分区，为一次病害确诊登记**可复用工具的分区到访、合格消毒事件及病害确诊**，按时间重放工具—分区之间的污染传播，输出每个分区的**最早暴露时间、最短事件链与待隔离清单**；补录 / 删除事件后自动重算，确认后**冻结案件快照**，支持筛选与 JSON 导出。
4. **作物积温预测**（`/heat`）：复用现有温室分区，为种植批次设置**基温、上限温度、目标积温与允许采样间隔**，录入带时间戳的温度；温度截断后对相邻读数做**梯形积分并按午夜拆分**，间隔超限**只登记缺口、不累计**；补录 / 删除读数后按时间全量重算，给出**每日积温、累计值、首次达标时刻与缺口清单**，未达标时以最近 7 个无缺口完整日的日均积温外推预计日期（样本不足明确不可预测）；页面展示温度 / 累计积温曲线与每日台账，支持按分区、批次筛选及 JSON 导出。
5. **滴灌均匀度巡检**（`/drip`）：复用现有阀门与分区，一次巡检选择**一条阀门**并为其下各分区配置**基准入口压力、设计滴头流量、DU 下限、尾首降幅阈值与低流量比例**，录入入口压力及**按首 / 中 / 尾编号**的滴头实测流量；每区至少 8 个样本，按流量升序取 `ceil(n/4)` 个计算**低四分位均值 DU**；超过半数分区同时「入口压力低于基准 + DU 合格」时整轮标记**共享供压异常**，未触发时按规则判**支管压损**与**疑似滴头堵塞**；补录 / 删除样本后整轮重算，输出每区 DU、判定依据与维护清单，支持历史筛选与 JSON 导出。
6. **肥料母液分罐**（`/fertilizer`）：复用现有温室分区，每份配方选择分区并设置 **A/B 罐容量（mL，正整数）**，录入**肥料名称、所需母液量（mL，非负整数；用量为 0 不参与求解）**及**两两不相容关系**；先按不相容图拆分**连通分量并二着色**，再在各分量的两种翻转方向中枚举全局组合，以**满足双罐容量、两罐体积差最小**为目标、同分按 **A 罐肥料 ID 升序序列**决胜；出现**奇环**时返回**最短冲突环**，容量无解时给出**所有组合中的最小总超额及对应分罐结果**（不只显示“无法分配”）；编辑用量 / 关系 / 容量后立即重算，页面展示分罐清单、占用量与冲突链，支持保存及导出 JSON。
7. **执行核验**（`/verify`）：按**计划日期**建立核验批次，导入按时间递增的**阀门实际运行事件**（唯一 `event_id`、阀门、开始 / 结束时间、实际流量；**同编号同内容重复提交幂等，同编号内容冲突整批拒绝**；运行允许中断、允许并发），系统按**左闭右开半开区间**把事件与当前轮灌计划逐段对齐：同一阀门的事件按与计划项的交集唯一归属分区，无法唯一归属（同时压多个计划项）的子段标为**证据不足、列出候选分区且不重复计量**；按实际流量核算各分区**已灌 / 缺水 / 超灌、提前 / 延后、中断、越界（计划外）水量**，逐分钟标出实际运行中的**阀门并发超限与总流量超限**；对缺水分区生成**不改原计划**的补灌建议，只使用**剩余允许时段**与停水、流量、并发约束（同时对原计划占用与实际运行占用做约束检查），无解时给出**最小缺口与原因**；补录 / 删除事件后**全量重算**，批次保存到 SQLite 并可导出 JSON。

## 一键启动（Docker Compose）

```bash
docker compose up --build -d
```

启动后访问：

- 轮灌编排台：**http://localhost:8000/**
- 水质监测页：**http://localhost:8000/water**
- 病害追踪页：**http://localhost:8000/disease**
- 积温预测页：**http://localhost:8000/heat**
- 滴灌巡检页：**http://localhost:8000/drip**
- 母液分罐页：**http://localhost:8000/fertilizer**
- 执行核验页：**http://localhost:8000/verify**

七个页面顶部有页签可互相跳转。数据通过 Docker 卷 `irrigation-data` 持久化（容器内 `/app/data/irrigation.db`）。停止与清理：

```bash
docker compose down          # 停止（数据保留）
docker compose down -v       # 停止并删除数据卷
```

## 不用 Docker 直接运行

需要 Python 3.10+：

```bash
pip install -r requirements.txt
python app.py
# 访问 http://localhost:8000/ 、/water 、/disease 、/heat 、/drip 、/fertilizer 、/verify
```

首次启动会自动建库并写入一套内置示例数据（轮灌示例 + 2 个水源的水质历史采样 + 1 个灰霉病病害追踪示例案件 + 2 个作物积温批次 + 2 轮滴灌均匀度巡检 + 1 份肥料母液分罐配方 + 1 个执行核验示例批次）。其中示例阀门已绑定示例水源并内置一份当日轮灌计划，打开 `/water` 的「用水回溯」即可直接看到风险 / 待复核 / 证据不足三类分区，打开 `/verify` 可直接看到提前 / 中断缺水 / 计划外 / 阀门并发 / 总流量冲突五类核验结论与补灌建议。

## 功能说明

### 数据录入（左侧面板）

- **阀门**：名称、额定流量（L/min）、**并发上限**（同一阀门同时最多开启几路，通常为 1）。
- **分区**：名称、**所需水量（L）**、**所属阀门**、**最早 / 最晚灌溉时段**（15 分钟对齐）。
  - 需要的时长按 `ceil(所需水量 / (阀门额定流量 × 15min))` 个槽位自动计算。
- **停水时段**：泵房检修等全局不可灌溉区间，可添加多条。
- **计划日期 / 总流量上限**（L/min）：顶部栏设置，改动后立即重新校验。

### 自动编排

点击 **「⚙ 自动编排」**，系统按「调度紧迫度」（允许窗口长度 − 需要槽数，越紧越先排，水量大者优先）贪心分配，对每个分区寻找第一个同时满足以下全部约束的连续槽段：

1. **总流量约束**：同一槽位所有开启分区的流量之和 ≤ 总流量上限；
2. **阀门并发约束**：同一阀门同一槽位开启的分区数 ≤ 该阀门并发上限；
3. **不可重叠 / 允许时段**：整块位于该分区最早–最晚时段内；
4. **停水时段约束**：不与任何停水区间相交。

找不到可行位置的分区标记为「未编排」，并给出诊断原因（如“允许时段全部处于停水时段”“总流量超限”等）。

### 时间轴与即时校验

- 每个分区一行，绿色虚线框为允许时段，灰色斜纹为停水覆盖，彩色色块为已排时段（按阀门着色，红色斜纹表示该段有冲突）。
- **拖动色块**即可调整该分区开始时间（自动吸附 15 分钟网格，自动钳制在 00:00–24:00 内），松手即整表重新校验并通过浮层提示结果。
- **点击色块**（或左侧分区条目）在左下角表单中精确修改：分区参数、以及「计划开始时间」字段；提交后按新水量/流量重算时长并即时校验。留空开始时间可取消该分区编排。
- 顶部状态栏显示校验结果与峰值流量；下方冲突面板逐条列出 `分区 + 时间段 + 具体原因`，例如：
  - 超出允许灌溉时段
  - 停水时段
  - 总流量超限（65 > 60 L/min）
  - 阀门 中部干管 并发超限（>1）
- 「💾 保存计划」将当前编排写入 SQLite（拖动/表单调整本身也已即时落库并校验，保存按钮可作为校验检查点）。状态栏同时显示当前**计划修订号**。

### 防覆盖修订与双窗口复核

计划（以及影响计划有效性的约束）带一个单调递增的 `plan_revision` 修订号，存于 SQLite `settings` 表：

- **加载取基线**：`GET /api/state` 返回 `revision`，页面据此记住基线修订。
- **提交带基线**：`PUT /api/schedule`（拖动 / 表单保存 / 💾 保存计划）与 `POST /api/generate`（自动编排）均携带 `base_revision`。服务端在单个 `BEGIN IMMEDIATE` 事务里执行「校验基线 → 整表替换 `schedule_items` → 修订 +1」：
  - 基线等于当前修订 → 原子提交，返回新修订；
  - 基线过期 → **409 `{"conflict":"revision","current_revision":…,"state":{最新完整状态}}`**，DELETE/INSERT 全部回滚，**不留下部分更新**；
  - 不带 `base_revision` 的请求按旧客户端兼容处理（单窗口直接写入），单窗口日常操作与既有校验 / 导出接口行为不变。
- **约束变更同样失效旧基线**：阀门**额定流量 / 并发上限**、分区**水量 / 所属阀门 / 允许时段**、**停水时段**、**总流量上限**任一变更（以及新增 / 删除阀门、分区、停水时段、清空 / 恢复示例）都在同一事务内递增修订，避免在过期约束上合并。仅改名、仅改计划日期、仅改阀门水源绑定**不**递增。
- **本地草稿与逐项合并**：提交被 409 拒绝后，页面不覆盖你的改动——时间轴切换为琥珀色「本地草稿」视图（色块加虚线描边），顶部出现草稿横幅；点 **🧾 逐项复核并提交**，弹窗按分区列出三方差异：
  - **仅本地**：默认保留本地版本（可改选服务器，相当于撤销本地改动）；
  - **仅服务器**：默认采用先提交者的服务器版本（可改选本地）；
  - **双方都改**：无默认值，操作员**必须逐项二选一**，未选完不能提交；两边改成相同结果时自动视为一致、无需选择；
  - 分区已被另一窗口删除时，本地时段标记「随服务器移除」，无法提交；
  - 若期间**约束也变了**（流量 / 分区 / 停水 / 总流量），弹窗顶部红色提示具体变更，提醒合并后按新约束再核对。
  - 提交仍以**服务器最新修订**为基线做 CAS；合并期间版本再次变化会继续 409、无部分更新，弹窗按最新状态重建差异并尽量保留已选项。
- **放弃草稿**：草稿横幅上的「放弃草稿」会丢弃本地改动、重新载入服务器最新计划。

### 双窗口复核步骤（页面 + curl）

沿用 SQLite 与现有 Docker Compose 启动方式（`docker compose up --build -d`，数据在 `irrigation-data` 卷，无需新增服务）。先以两个浏览器窗口（或同一窗口 + curl 模拟另一窗口）复核：

```bash
# 0) 恢复示例，记下当前修订号（初始为 1）
curl -s -X POST http://localhost:8000/api/reset -H 'Content-Type: application/json' \
  -d '{"sample":true}' > /dev/null
curl -s http://localhost:8000/api/state | python3 -c 'import sys,json;print("revision =",json.load(sys.stdin)["revision"])'
```

页面侧：

1. 分别在**窗口 A、窗口 B** 打开 `/`，两者状态栏均显示「修订 1」。
2. 在窗口 A 把**番茄区**色块向右拖动一格（或表单改开始时间）→ 松手即落库，修订变为 2，提示校验通过。
3. 回到窗口 B，把**黄瓜区**拖到另一时刻松手 → 收到红色提示「计划修订已过期：本地草稿已保留，请逐项复核差异后再提交」：
   - 时间轴变为琥珀色草稿视图，番茄区显示窗口 A 已提交的版本、黄瓜区保留你刚才的拖动；
   - 顶部横幅显示「本地草稿基于修订 1，服务器已到修订 2」。
4. 点 **🧾 逐项复核并提交**：弹窗中番茄区行为「仅服务器」（默认选服务器）、黄瓜区行为「仅本地」（默认选本地）；保持默认直接提交 → 成功，修订变为 3，两个窗口的改动都在，状态栏恢复绿色校验结论。
5. 「双方都改」复核：两窗口再各拖**同一个分区**（如草莓区）到不同时刻，后提交的窗口弹窗中该分区标记红色「双方都改」，**必须**手动选择本地或服务器，未选时提交按钮禁用；选择后才提交。
6. **约束失效**复核：窗口 A 把顶部总流量上限改小（或编辑阀门额定流量 / 新增停水时段）；窗口 B 再拖动提交 → 弹窗顶部出现红色「⚠ 约束已变更：总流量上限 60 → 50 L/min。…」，按提示合并后查看冲突面板。
7. **合并期间再变**：弹窗打开未提交时，用 curl 再改一次计划，点提交应再次被拒、弹窗刷新为最新差异，数据库无部分更新。
8. 单窗口日常使用（只有一个窗口打开时拖动 / 自动编排 / 保存 / 校验 / 导出）流程与旧版完全一致。

API 侧复核：

```bash
# 恢复示例后分区 ID 固定：番茄区=1、黄瓜区=2、草莓区=3、叶菜区=4、育苗区=5
curl -s -X POST http://localhost:8000/api/reset -H 'Content-Type: application/json' \
  -d '{"sample":true}' > /dev/null
# 全新库初始修订为 1；/api/reset 本身也会递增，恢复示例后读到下一个修订号
R=$(curl -s http://localhost:8000/api/state | python3 -c 'import sys,json;print(json.load(sys.stdin)["revision"])')
echo "当前基线修订 R=$R"

# 窗口 A 先提交（番茄区挪到 08:30 起，其余时段沿用示例计划）：200，修订 +1
curl -s http://localhost:8000/api/state | python3 -c '
import sys,json,urllib.request
s=json.load(sys.stdin)
items=[{"zone_id":i["zone_id"],
        "start_slot":34 if i["zone_id"]==1 else i["start_slot"],
        "length":i["length"]} for i in s["items"]]
req=urllib.request.Request("http://localhost:8000/api/schedule",
    data=json.dumps({"base_revision":s["revision"],"items":items}).encode(),
    headers={"Content-Type":"application/json"},method="PUT")
print("窗口 A:",urllib.request.urlopen(req).status)'

# 窗口 B 用同一旧基线提交（黄瓜区 19:00 起）：409，响应附最新 state
curl -s -o /tmp/conflict.json -w '窗口 B: %{http_code}\n' -X PUT http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  -d "{\"base_revision\":$R,\"items\":[]}"
python3 -c 'import json;d=json.load(open("/tmp/conflict.json"));print(d["conflict"],"当前修订 =",d["current_revision"])'

# 用最新修订号提交合并稿 -> 200；自动编排接口同样支持 base_revision
NEW=$(python3 -c 'import json;print(json.load(open("/tmp/conflict.json"))["current_revision"])')
curl -s -o /dev/null -w '合并提交: %{http_code}\n' -X POST http://localhost:8000/api/generate \
  -H 'Content-Type: application/json' -d "{\"base_revision\":$NEW}"
# 旧基线再调自动编排 -> 409 且计划不变（无部分更新）
curl -s -o /dev/null -w '过期编排: %{http_code}\n' -X POST http://localhost:8000/api/generate \
  -H 'Content-Type: application/json' -d "{\"base_revision\":$R}"

# 约束变更使旧基线失效：改总流量后用旧修订号保存 -> 409
curl -s -o /dev/null -X POST http://localhost:8000/api/settings \
  -H 'Content-Type: application/json' -d '{"total_flow":"80"}'
curl -s -o /dev/null -w '约束变更后的旧基线: %{http_code}\n' -X PUT http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' -d "{\"base_revision\":$R,\"items\":[]}"
# 不带 base_revision 的旧客户端：200（单窗口兼容）
curl -s -o /dev/null -w '无基线旧客户端: %{http_code}\n' -X PUT http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' -d '{"items":[]}'
# 导出 JSON 携带 plan_revision
curl -s http://localhost:8000/api/export | python3 -c 'import sys,json;print("导出修订 =",json.load(sys.stdin)["plan_revision"])'
```

后端回归（无需容器，宿主机 Python 3.10+）：

```bash
python3 -m unittest test_schedule_revision -v
# 覆盖：先提交者胜出 / 409 无部分更新 / 合并期间再变继续拒绝 /
#       阀门流量·分区·停水·总流量变更失效基线 / 仅改名不失效 / 旧客户端无基线兼容 / 校验与导出兼容
```

数据库迁移说明：修订号存于既有 `settings` 表的 `plan_revision` 键（无需新表 / 卷，沿用现有 SQLite 与 Docker Compose 数据卷）；旧库首次启动经 `PRAGMA user_version=3` 迁移幂等补入 `plan_revision=1`，已有计划继续可用。

### 导出 / 重置

- **「⬇ 导出 JSON」**：下载当前计划（含计划日期、生成时间、流量上限、实际峰值、是否有效、停水时段、各分区起止时间/时长/冲突原因、未编排分区及原因），文件名形如 `irrigation-plan-2026-09-21.json`。
- **「↺ 恢复示例」**：清空并重建内置示例（5 个温室分区、3 条阀门干管、1 条泵房检修停水）。
- **「🗑 清空重置」**：清空全部阀门 / 分区 / 停水时段 / 计划，从零开始。

## 灌溉水质连续监测（/water）

### 数据模型

- **水源**：名称唯一，可带备注（如「1# 机井水」「雨水收集池」）。
- **阀门绑定**：每个阀门可绑定**一个**水源（水质页左侧「阀门水源绑定」）。用水回溯按阀门所绑水源核算；删除水源时绑定自动解除，相关分区改标证据不足。
- **正常区间**：每个水源独立配置四项指标的下限/上限，默认 pH 5.5~8.5、EC 0~2.0 mS/cm、浊度 0~10 NTU、硝酸盐 0~30 mg/L。修改区间后对该水源全部历史采样**按时间整体重算**。
- **采样**：每条采样带本地时间戳，四项指标缺一不可，指标单位固定（EC 仅接受 `mS/cm`、浊度仅接受 `NTU`、硝酸盐仅接受 `mg/L`、pH 为无量纲值，带任何单位均拒绝）。

### 状态规则（按水源、按指标、按时间）

- 某指标采样值**连续 2 次越出正常区间** → 该指标进入「🔴 超标」；
- 超标后采样值**连续 2 次回到区间内** → 恢复「🟢 正常」；
- 只越界 1 次、或恢复途中只回归 1 次 → 「🟠 观察」；
- 水源整体状态取四项指标中的最差者，观察/超标时**列出触发指标**（如「硝酸盐」「浊度」）。

### 录入校验（拒绝非法请求）

- **拒绝缺项**：四项指标少任意一项、空字符串、非数字、超出物理可能范围（如 pH=15）均返回 400。
- **拒绝非法单位**：只接受上表规定的单位字符串。
- **拒绝乱序批次**：批量提交（`{"batch": [...]}`）时，批内时间戳必须严格递增；首批时间戳不得早于或等于该水源库内最新采样；同一水源同一时间戳唯一。任一条目不合法则**整批回滚**，不会写入半批数据。
- 时间戳只接受**本地时间**（`YYYY-MM-DDTHH:MM:SS`，兼容空格分隔），带 `Z`/`UTC`/时区偏移的一律拒绝，避免跨时区排序歧义。

### 筛选 / 趋势 / 导出

- 顶部可按**水源**与**日期**筛选；默认展示该水源**最近 50 条**采样。
- 四张内联 SVG 趋势图分别绘制四项指标，灰色色带为正常区间，数据点按该指标当时状态着色（绿=正常 / 橙=观察 / 红=超标），悬停可见数值与状态；越界数值在明细表中红色标出。
- **「⬇ 导出 JSON」**按当前筛选条件导出（含各水源当前状态、区间配置、单位、采样明细及每个采样点的指标/整体状态），文件名形如 `water-quality-3-all.json` 或 `water-quality-3-2026-09-21.json`。
- 删除某条采样后同样会按时间重算该水源后续状态。

### 用水回溯（按计划日期）

水质异常后，在水质页中部「用水回溯」选择**计划日期**（默认取轮灌编排台的计划日期），系统把该日现有 **15 分钟灌溉时段**与各绑定水源按「连续两次」规则重建的**半开状态区间**求交，列出当天用了风险水的轮灌分区。回溯是按需实时核算的，**只读取计划、不改动原计划**。

**区间与边界规则**

- 状态在**采样时刻起生效**并持续到下一条采样：每条采样对应左闭右开区间 `[ts_i, ts_{i+1})`，末条状态向后延续；因此灌溉时段在采样时刻整点切换状态（采样落在 15 分钟槽中间时按分钟切分，重叠分钟允许小数）。
- **当日首条采样之前**的灌溉时段：沿用当日之前最近一条历史采样的状态，结果标注来源为「历史采样 …」。
- 该水源在当日之前**没有任何历史样本**（当日第一条就是全水源第一条）时，首条采样之前标为**证据不足**，不出具状态结论，仅登记涉及水量待补采样复核。
- 当日没有采样但有历史样本时，全天沿用最近历史状态。
- 交集落在「超标」区间计为 **风险**（红），落在「观察」区间计为 **待复核**（橙）；**阀门未绑定水源**（或阀门已删除）的灌溉时段整段标为证据不足。
- **水量 = 阀门额定流量 (L/min) × 重叠分钟数**。一个分区跨多个状态区间时逐段拆分；分区结论取最差归类（风险 > 待复核 > 证据不足 > 正常），触发指标为各段非「正常」指标的并集。
- 每行展开显示重叠子区间及其**状态来源采样**（当日采样 / 历史采样的时间戳，或「无历史样本」「阀门未绑定水源」）。
- **补录、删除采样、修改区间或修改阀门绑定后，再次核算自动按新数据重建**；页面支持按单个分区筛选，并可「⬇ 导出回溯 JSON」（含日期、汇总水量、未绑定阀门清单与每分区重叠明细），接口为 `GET /api/water/trace?date=YYYY-MM-DD&zone_id=…`、导出 `GET /api/water/trace/export`。

**复核示例（内置示例数据）**

- 计划日期当天，1# 机井水硝酸盐已连续两次超标（昨日 08:00、14:00），今日 08:00、09:00 采样仍超标：
  - 草莓区 05:30–06:30 在今日首条采样之前，**沿用昨日 14:00 的「超标」状态** → 风险 1500 L（25 L/min × 60 min），来源标注「历史采样 …14:00」；
  - 番茄区 08:00–10:00 与 08:00、09:00 两条超标采样区间求交 → 风险 3000 L，来源为当日采样；
  - 黄瓜区 18:00–19:30 沿用当天最后一条（09:00）超标状态 → 风险 2250 L。
- 雨水收集池浊度昨日越界 1 次、今日 07:30 回到区间（仅 1 次）→ 整体为「观察」。把中部干管改绑到该水源后重新核算，草莓 / 黄瓜 / 叶菜三区即由风险变为**待复核**，触发指标为「浊度」。
- 苗床支管未绑定水源，其育苗区 08:00–08:45 灌溉段标为**证据不足**（不计入风险 / 待复核水量，顶部提示未绑定阀门）；绑定后再次核算立即出结论。

## 病害接触追踪（/disease）

围绕一次病害确诊开一个**案件**（如「灰霉病」），登记三类事件，系统按时间整体重放：

| 事件 | 必填对象 | 含义 |
|---|---|---|
| 🔴 确诊 `diagnosis` | 分区 | 该分区确诊为污染**源头**，立即具有传染性（不经过潜伏期） |
| 🔵 到访 `visit` | 工具 + 分区 | 可复用工具进入某温室分区的一次到访记录 |
| 🟢 消毒 `disinfection` | 工具 | 标记消毒是否**合格**；只有合格消毒才截断传播 |

### 传播重放规则

- 工具**进入已污染分区**后被污染；该工具**之后到访的分区立即列为暴露**。
- 暴露分区经过案件配置的**潜伏时长**（小时，可改 0 表示立即传染）后，才可继续污染到访的其他工具；确诊源头分区不受潜伏期限制。
- 工具完成**对应病害的合格消毒**后截断其传播链（消毒后到访不再传播，直到再次被污染）；**不合格消毒不截断**。
- **同一时刻按「确诊 → 到访 → 消毒」顺序处理**，同类型事件按事件 ID 排序。
- 循环传播只保留**最早暴露**；暴露时间相同时保留**更短事件链**；等长事件链按**事件 ID** 字典序决胜。

### 结果与操作

- **分区暴露结果表**：每个分区的状态（确诊源头 / 暴露潜伏中 / 已具传染性 / 清洁）、最早暴露时间、具备传染性时间，以及从确诊源头一路展开的**最短事件链**（芯片显示每次到访 / 消毒）。
- **🛑 待隔离清单**：所有暴露分区（含确诊源头），标注最早暴露时间与潜伏期是否已到。
- 表格同时给出工具在「重放截止时间」是否仍携带污染。
- **补录或删除事件**（包括改潜伏时长）后自动按时间整体重算；页面上方的「事件时间线」逐条给出每次事件的推演说明（如「仍在潜伏期中，工具未被污染」「消毒不合格，工具仍携带污染」）。
- **🔒 确认并冻结案件快照**：把当前重算结果（最早暴露、最短事件链、待隔离清单）固化到 SQLite；冻结时同时把各事件引用工具的**编号与名称固化进事件台账**。冻结后案件只读，不能再增删改事件或潜伏时长，导出始终返回冻结时的快照。
- **历史工具引用完整性**：删除工具时，若它仍被**未冻结（开放）案件**的事件引用，接口返回 400 拒绝删除（请先冻结案件）；案件**已冻结**后允许删除该工具，删除**不会**改写冻结案件的历史事件——事件台账、传播链、待隔离清单与 JSON 导出仍保留原工具编号与名称，已删除工具在工具列表中以「已删除 · 冻结案件历史」标记、仍可用于按工具筛选。「已停用（退休）」语义不变：退休只是标记、工具仍在列表中。为保证重启后仍可查询，事件表冗余保存 `tool_name`，旧库启动时自动迁移（见下文复核步骤）。
- 顶部可按**事件类型 / 工具 / 分区**筛选事件与结果，并可勾选「仅看待隔离分区」；**⬇ 导出 JSON** 导出当前案件的案件信息、分区、工具、全部事件与重放结果（已冻结案件导出快照）。

### 操作示例（页面）

1. 在左侧「案件」卡填 **病害名称（灰霉病）+ 潜伏时长（24 小时）→ 新建案件**。
2. 在「可复用工具」卡登记 **修枝剪 A、采收筐 1 号**（直接复用轮灌台已有的番茄区 / 黄瓜区等分区）。
3. 按时间依次登记：
   - `09-24 09:00 确诊 番茄区`
   - `09-24 10:00 到访 修枝剪 A → 番茄区`（进入污染分区，工具被污染）
   - `09-24 11:00 到访 修枝剪 A → 黄瓜区`（黄瓜区立即暴露，链：确诊 ➜ 番茄区到访 ➜ 黄瓜区到访）
   - `09-24 12:00 到访 采收筐 1 号 → 黄瓜区`（黄瓜区仍在 24 h 潜伏期，工具**不**被污染）
   - `09-24 12:30 消毒 修枝剪 A（不合格，不勾选）` → 13:00 再到访叶菜区仍会传播
   - `09-24 16:00 消毒 修枝剪 A（合格，勾选）` → 之后到访育苗区不再传播
4. 把某条合格消毒事件**删除**，可见后续分区的最早暴露时间立即提前、事件链变化。
5. 核对「待隔离清单」与最短事件链无误后点 **🔒 确认并冻结案件快照**，再 **⬇ 导出 JSON** 留档。

### 操作示例（API）

```bash
# 新建案件（潜伏 24 小时）
curl -X POST http://localhost:8000/api/disease/cases \
  -H 'Content-Type: application/json' \
  -d '{"disease_name":"灰霉病","latency_hours":24}'

# 登记确诊 / 到访 / 合格消毒（事件 id 由后端生成，时间为本地时间）
curl -X POST http://localhost:8000/api/disease/cases/1/events -H 'Content-Type: application/json' \
  -d '{"type":"diagnosis","ts":"2026-09-24T09:00:00","zone_id":1}'
curl -X POST http://localhost:8000/api/disease/cases/1/events -H 'Content-Type: application/json' \
  -d '{"type":"visit","ts":"2026-09-24T10:00:00","tool_id":1,"zone_id":1}'
curl -X POST http://localhost:8000/api/disease/cases/1/events -H 'Content-Type: application/json' \
  -d '{"type":"disinfection","ts":"2026-09-24T16:00:00","tool_id":1,"qualified":true}'

# 查看重放结果（每个分区最早暴露 / 最短链 / 待隔离清单）
curl 'http://localhost:8000/api/disease/state?case_id=1'

# 删除事件后自动重算；确认无误冻结快照；导出 JSON
curl -X DELETE http://localhost:8000/api/disease/events/3
curl -X POST http://localhost:8000/api/disease/cases/1/freeze
curl -OJ 'http://localhost:8000/api/disease/export?case_id=1'
```

首次启动内置一套病害示例案件（灰霉病，14 个事件，含潜伏门控、合格 / 不合格消毒、循环只留最早暴露三类情形）。

### 冻结案件删除工具后的复核步骤

沿用 Docker Compose 启动（`docker compose up --build -d`，数据在 `irrigation-data` 卷），依次复核：

```bash
# 1) 准备：新建案件、登记至少一条到访事件后冻结（下例假设案件 id=1、工具 id=1）
curl -X POST http://localhost:8000/api/disease/cases \
  -H 'Content-Type: application/json' -d '{"disease_name":"灰霉病","latency_hours":0}'
curl -X POST http://localhost:8000/api/disease/cases/1/events -H 'Content-Type: application/json' \
  -d '{"type":"diagnosis","ts":"2026-09-26T09:00:00","zone_id":1}'
curl -X POST http://localhost:8000/api/disease/cases/1/events -H 'Content-Type: application/json' \
  -d '{"type":"visit","ts":"2026-09-26T10:00:00","tool_id":1,"zone_id":1}'
curl -X POST http://localhost:8000/api/disease/cases/1/freeze

# 2) 冻结案件的事件台账先记下工具编号与名称
curl -s 'http://localhost:8000/api/disease/state?case_id=1' \
  | python3 -c 'import sys,json; s=json.load(sys.stdin); print([(e["id"],e["tool_id"],e["tool_name"]) for e in s["events"] if e["tool_id"]])'

# 3) 删除工具：冻结案件应 200；若案件未冻结应返回 400
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE http://localhost:8000/api/disease/tools/1   # 期望 200

# 4) 复核四件套均未丢失工具编号/名称：
#    a) 事件台账 tool_id/tool_name 仍在；
#    b) result.zones[*].chain（传播链）芯片仍显示工具名；
#    c) result.quarantine（待隔离清单）与删除前一致；
#    d) 工具列表中该工具带 "deleted": true 标记。
curl -s 'http://localhost:8000/api/disease/state?case_id=1' \
  | python3 -c 'import sys,json; s=json.load(sys.stdin); assert all(e["tool_name"] for e in s["events"] if e["tool_id"]); print("ledger ok"); print("deleted tool:", [t for t in s["tools"] if t.get("deleted")])'

# 5) JSON 导出同样保留
curl -s 'http://localhost:8000/api/disease/export?case_id=1' \
  | python3 -c 'import sys,json; p=json.load(sys.stdin); assert any(e["tool_name"] for e in p["events"]); assert any(t.get("deleted") for t in p["tools"]); print("export ok")'

# 6) 重启后再次查询，结果应与第 4、5 步一致（迁移与冗余名称已持久化）
docker compose restart
curl -s 'http://localhost:8000/api/disease/state?case_id=1' \
  | python3 -c 'import sys,json; s=json.load(sys.stdin); print([(e["id"],e["tool_id"],e["tool_name"]) for e in s["events"] if e["tool_id"]])'
```

页面侧复核：冻结案件后删除其工具，回到 `/disease` 选中该冻结案件——事件明细表「工具」列、时间线标题、分区结果表「最短事件链」芯片、🛑 待隔离清单仍显示原工具名；左侧工具列表该工具显示「已删除 · 冻结案件历史」，顶部「工具」筛选选中它仍能筛出历史事件；事件登记表单的工具下拉不再包含已删除工具。退休（已停用）工具仍在列表、标记不变。

数据库迁移说明：旧版 `disease_events` 表的 `tool_id` 带外键、无 `tool_name` 列；首次启动新版本时按 `PRAGMA user_version` 自动执行一次性迁移——重建表（去掉工具外键、新增 `tool_name` 并从工具表回填名称、保留分区外键与索引），并尽力从已有冻结快照的传播链中还原历史上已被置空的事件引用；迁移幂等，完成后 `user_version=1`。

## 作物积温预测（/heat）

### 数据模型

- **种植批次**：名称 + 所属温室分区（复用轮灌台分区）+ 作物（可空），并为每个批次设置：
  - **基温 Tb**（℃）：低于基温的温度不计积温；
  - **上限温度 Tc**（℃）：高于上限的温度按上限计（封顶）；
  - **目标积温**（℃·日）：首次达到该累计值的时刻即「首次达标时刻」；
  - **允许采样间隔**（小时）：相邻读数间隔超过它即登记缺口。
- **温度读数**：带本地时间戳（`YYYY-MM-DDTHH:MM:SS`），录入**真实温度**；同一批次时间戳唯一，支持**补录历史读数**。

### 计算规则（每次写入 / 删除后按时间全量重算）

1. **温度截断**：每条读数的有效温度 = `min(Tc, max(Tb, temp)) − Tb`，有效积温始终非负。
2. **间隔门控**：相邻读数间隔 **≤ 允许采样间隔**才参与积分；**超过则只在缺口清单登记（起止、时长），缺口区间不累计任何积温**（间隔恰好等于阈值不算缺口）。
3. **梯形积分 + 按午夜拆分**：有效区间按 `两端有效温度均值 × 时长(小时) / 24` 计算 ℃·日；区间跨越午夜时在午夜点按线性插值切分，两段分别计入相邻两个自然日。
4. **首次达标时刻**：按时间顺序在每个子区间内解积分方程（二分求根），精确到秒；结果同时在日台账中标出首次达标日。
5. **每日台账**：每日积温、当日结束累计值、有效覆盖时长、缺口时长 / 段数、是否为**无缺口完整日**（24 小时全部被有效区间覆盖）。
6. **未达标外推**：取**最近 7 个无缺口完整日**的日均积温，从最后一条读数时间线性外推预计达标时刻与日期；完整日不足 7 个、或日均积温为 0 时，页面与 JSON 明确给出**不可预测**原因。

### 页面 / 导出

- 顶部按**分区**与**批次**两级筛选；批次列表卡显示累计 / 目标、缺口数、完整日数、达标状态。
- 三张内联 SVG 图：**温度曲线**（实测蓝线 + 截断后绿虚线，基温 / 上限参考线，红色阴影为缺口区间，越限读数红点）、**累计积温曲线**（目标线、首次达标竖线、外推预计日期标注）、**每日积温柱状图**（黄色柱为含缺口日）。
- **⏭ 缺口清单**、**每日积温台账**与**读数明细**（含截断后有效温度，可逐条删除）。
- **⬇ 导出 JSON**：导出分区、全部批次的参数与汇总、当前批次的完整重算结果（参数、读数、日台账、缺口、首次达标与外推依据），文件名形如 `crop-heat-batch-1.json`。
- **删除保护**：分区下仍有积温批次时，轮灌台删除分区返回 400，提示先删除批次（批次删除级联其读数）。

### 操作示例（API）

```bash
# 1) 新建批次：基温 10、上限 30、目标 150 ℃·日、允许采样间隔 13 小时（挂在分区 1 番茄区）
curl -X POST http://localhost:8000/api/heat/batches \
  -H 'Content-Type: application/json' \
  -d '{"name":"越冬番茄一批","crop":"番茄","zone_id":1,
       "base_temp":10,"cap_temp":30,"target_gdd":150,"max_gap_hours":13}'

# 2) 批量录入温度（也可补录历史时间戳；时间戳必须本地时间、批内不重复）
curl -X POST http://localhost:8000/api/heat/batches/1/readings \
  -H 'Content-Type: application/json' \
  -d '{"batch":[
    {"ts":"2026-09-17T08:00:00","temp":8},
    {"ts":"2026-09-17T20:00:00","temp":22},
    {"ts":"2026-09-18T08:00:00","temp":9},
    {"ts":"2026-09-18T20:00:00","temp":24}
  ]}'

# 3) 查看按时间全量重算的结果（按分区筛选加 &zone_id=1）
curl 'http://localhost:8000/api/heat/state?batch_id=1'

# 4) 修改参数（如把上限改为 28 ℃）或删除某条读数 -> 自动全量重算
curl -X PUT http://localhost:8000/api/heat/batches/1 \
  -H 'Content-Type: application/json' \
  -d '{"name":"越冬番茄一批","crop":"番茄","zone_id":1,
       "base_temp":10,"cap_temp":28,"target_gdd":150,"max_gap_hours":13}'
curl -X DELETE http://localhost:8000/api/heat/readings/3

# 5) 导出 JSON（含日台账 / 缺口 / 首次达标 / 外推依据）
curl -OJ 'http://localhost:8000/api/heat/export?batch_id=1'
```

首次启动内置两个积温批次：番茄区批次（每天 08:00 / 20:00 两条、含 32 ℃ 越限截断读数，8 个完整日、未达标，可看到外推预计日期）与黄瓜区补录批（含一个 18 小时漏测缺口、完整日不足 7 个，明确显示不可预测与缺口清单）。

### 复核示例（截断 / 午夜拆分 / 缺口 / 外推）

沿用 Docker Compose 启动（`docker compose up --build -d`），用内置番茄批次复核：

```bash
# 1) 番茄批次（默认 id=1）：未达标、无缺口、完整日 ≥ 7 -> 应返回 predictable=true
curl -s 'http://localhost:8000/api/heat/state?batch_id=1' | python3 -c '
import sys, json
s = json.load(sys.stdin)["selected"]["result"]
print("累计:", s["total_gdd"], "缺口:", s["gap_count"], "完整日:", s["complete_day_count"])
f = s["forecast"]
assert f and f["predictable"], f
print("最近 7 个完整日日均积温:", f["avg_daily_gdd"], "℃·日")
print("预计达标:", f["predicted_date"], f["predicted_at"], "样本:", f["sample_dates"][0], "~", f["sample_dates"][-1])
'

# 2) 手工复核一条跨午夜积分：录入两条恒温 22 ℃、间隔 12 小时（有效温度 12 ℃）
#    当日/次日各分 6 小时 -> 各 +3.0 ℃·日
curl -X POST http://localhost:8000/api/heat/batches/1/readings -H 'Content-Type: application/json' \
  -d '{"ts":"2027-01-10T18:00:00","temp":22}'
curl -X POST http://localhost:8000/api/heat/batches/1/readings -H 'Content-Type: application/json' \
  -d '{"ts":"2027-01-11T06:00:00","temp":22}'
curl -s 'http://localhost:8000/api/heat/state?batch_id=1' | python3 -c '
import sys, json
d = {x["date"]: x for x in json.load(sys.stdin)["selected"]["result"]["daily"]}
assert abs(d["2027-01-10"]["gdd"] - 3.0) < 1e-6 and abs(d["2027-01-11"]["gdd"] - 3.0) < 1e-6
print("午夜拆分复核通过：2027-01-10 +3.0、2027-01-11 +3.0 ℃·日")
'

# 3) 造一个超间隔缺口（间隔 18h > 13h 允许间隔）：缺口登记、区间不累计
curl -X POST http://localhost:8000/api/heat/batches/1/readings -H 'Content-Type: application/json' \
  -d '{"ts":"2027-01-12T06:00:00","temp":24}'   # 与上一条相差正好 24h
curl -s 'http://localhost:8000/api/heat/state?batch_id=1' | python3 -c '
import sys, json
r = json.load(sys.stdin)["selected"]["result"]
g = r["gaps"][-1]
print("缺口:", g["start_ts"], "->", g["end_ts"], g["duration_hours"], "h（不累计积温）")
assert g["duration_hours"] == 24.0
'

# 4) 黄瓜补录批（默认 id=2）：有缺口且完整日 < 7 -> 必须明确不可预测
curl -s 'http://localhost:8000/api/heat/state?batch_id=2' | python3 -c '
import sys, json
f = json.load(sys.stdin)["selected"]["result"]["forecast"]
assert not f["predictable"] and "不足" in f["reason"]
print("样本不足复核通过：", f["reason"])
'

# 5) 把目标积温调到当前累计以下 -> 立即出现首次达标时刻、不再外推；删除读数后又全量重算
curl -s 'http://localhost:8000/api/heat/state?batch_id=1' \
  | python3 -c 'import sys,json;print("当前累计:", json.load(sys.stdin)["selected"]["result"]["total_gdd"])'
curl -OJ 'http://localhost:8000/api/heat/export?batch_id=1'
```

页面侧复核：进入 `/heat`，在左上方筛选器选「黄瓜区」，右侧应出现黄色「⛔ 不可预测：无缺口完整日仅 N 个，不足 7 个，样本不足以外推」，下方「⏭ 缺口清单」列出漏测区间；选回「番茄区」可见绿色外推卡片、温度曲线上的基温/上限虚线与 32 ℃ 红点（按 30 ℃ 截断）、累计曲线上的预计达标标注；在左侧把「目标积温」改小后保存，顶部变为「已达标」并给出首次达标时刻，日台账对应日期高亮。

## 滴灌均匀度巡检（/drip）

复用轮灌台的**阀门与分区**，围绕一条阀门开一轮巡检（如「秋茬滴灌首轮巡检」），录入入口压力与按首 / 中 / 尾编号的滴头实测流量，整轮自动重算 DU 与异常判定。

### 数据模型

- **巡检轮次（drip_rounds）**：名称 + **巡检阀门（一轮一条阀门）** + 巡检时间（本地时间）+ 备注。
- **巡检分区（drip_zones）**：轮次下该阀门的每个分区一行，保存五项巡检参数与实测入口压力，并冗余分区名称快照（分区日后改名 / 删除不影响历史台账）：
  - **基准入口压力**（MPa）：入口压力 `<` 基准即供压偏低；
  - **设计滴头流量**（L/h）：滴头额定流量；
  - **DU 下限**（如 0.80）：DU `≥` 下限为合格；
  - **尾首降幅阈值**（如 0.10）：尾段中位数相对首段降幅 `≥` 阈值视为衰减显著；
  - **低流量比例**（如 0.70）：实测流量 `< 设计流量 × 该比例` 的采样点列为疑似堵塞。
- **滴头样本（drip_samples）**：每个样本含**管段位置**（`head` 首段 / `middle` 中段 / `tail` 尾段）、**滴头编号**（各段独立编号，如 H01 / M03 / T02）与**实测流量**（L/h）。同分区同段编号唯一，支持补录历史。

### DU 与整轮判定规则

1. **每区至少 8 个样本**才计算 DU；不足 8 个判「样本不足」，不参与整轮多数统计，维护清单提示补录。
2. **低四分位均值 DU**：样本按流量升序排列，取前 `ceil(n/4)` 个求均值，
   `DU = 低四分位均值 / 全样本均值`（均值为 0 时 DU 记 0）。
3. **尾首降幅** = `(首段流量中位数 − 尾段流量中位数) / 首段流量中位数`（首 / 尾段各取该段全部样本的中位数）。
4. **共享供压异常**：整轮中**严格超过半数**分区同时满足「入口压力低于基准」**且**「DU 合格（≥ 下限）」→ 整轮标记 `shared_pressure`，命中分区标注为「共享供压异常涉及分区」，整轮不再单列支管压损 / 滴头堵塞，维护清单只保留一条泵房 / 干管供压核查项。**入口压力未录入的分区不计入供压偏低**（也不参与共享候选），维护清单单列「补测入口压力」提醒。
5. **未触发共享供压异常时的分区判定**：
   - 入口压力合格（`≥` 基准）+ DU 不合格 + 尾首降幅 `≥` 阈值 → **支管压损**（尾段低压导致的不均匀；该分区低流量点按压损解释，**不**列滴头堵塞）；
   - 入口压力低于基准但 DU 合格 → **入口压力偏低**（同时是共享供压异常候选分区）；
   - 其余 DU 不合格 → **均匀度异常**，提示现场分段测压复核；
   - DU 合格 → **合格**。
6. **疑似滴头堵塞**：未触发共享供压异常、且分区**不是**支管压损时，所有实测流量 `< 设计流量 × 低流量比例` 的采样点逐条列入维护清单（含分区、首/中/尾段、编号与阈值依据）。

每次**新建 / 修改轮次参数、改某分区参数或入口压力、补录 / 删除样本**后，都取整轮全部分区与全部样本**整体重算**（计算模块无状态、结果不缓存）。

### 页面 / 导出

- 左侧：巡检轮次历史列表（按时间倒序，显示阀门、判定徽标、已评分区 / DU 合格数 / 样本数）、新建 / 编辑轮次表单（按所选阀门自动展开其下全部分区的六宫格参数）、滴头样本录入（分区 + 首/中/尾 + 编号 + 流量）。
- 右侧：整轮总览（命中分区比例、判定规则原文）、**🛠 维护清单**（高/中/低优先级，含处理动作与判定依据）、**分区判定结果卡**（DU、低四分位均值、全样本均值、首/中/尾中位数、尾首降幅、堵塞阈值、逐条判定依据、疑似堵塞芯片，可折叠，可就地修改参数）、**滴头样本明细**（按分区 / 管段 / 仅低流量点筛选，可逐条删除）。
- 顶部支持按**阀门**与**巡检日期**筛选历史；**⬇ 导出 JSON**：选中轮次时导出该轮完整结果（含每区样本与判定依据），处于筛选状态时导出符合条件的轮次列表。
- **删除保护**：分区在巡检轮次中有记录时，轮灌台删除分区返回 400；阀门下仍有巡检轮次（或绑定分区）时禁止删除。轮次删除级联其分区行与全部样本。

### 操作示例（API）

```bash
# 1) 新建巡检轮次（阀门 2 中部干管；为分区 2/3/4 各配五项参数 + 实测入口压力）
curl -X POST http://localhost:8000/api/drip/rounds \
  -H 'Content-Type: application/json' \
  -d '{"name":"秋茬滴灌首轮巡检","valve_id":2,"inspected":"2026-09-27T09:00",
       "zones":[
         {"zone_id":2,"inlet_pressure":0.12,"base_inlet_pressure":0.10,"design_flow":2.0,
          "du_min":0.80,"tail_drop_limit":0.10,"low_flow_ratio":0.70},
         {"zone_id":3,"inlet_pressure":0.11,"base_inlet_pressure":0.10,"design_flow":2.0,
          "du_min":0.80,"tail_drop_limit":0.10,"low_flow_ratio":0.70},
         {"zone_id":4,"inlet_pressure":0.08,"base_inlet_pressure":0.10,"design_flow":2.0,
          "du_min":0.80,"tail_drop_limit":0.10,"low_flow_ratio":0.70}
       ]}'

# 2) 为分区录入滴头样本（单条或 {"batch":[...]}；位置 head/middle/tail，编号同段唯一）
curl -X POST http://localhost:8000/api/drip/zones/1/samples \
  -H 'Content-Type: application/json' \
  -d '{"batch":[
    {"position":"head","label":"H01","flow":1.95},
    {"position":"head","label":"H02","flow":2.00},
    {"position":"tail","label":"T01","flow":1.20}
  ]}'

# 3) 查询整轮重算结果（每区 DU / 判定依据 / 维护清单）
curl 'http://localhost:8000/api/drip/state?round_id=1'

# 4) 历史筛选（阀门 + 巡检日期）；改入口压力 / 参数；删除样本后整轮重算
curl 'http://localhost:8000/api/drip/state?valve_id=2&date=2026-09-27'
curl -X PUT http://localhost:8000/api/drip/zones/1 \
  -H 'Content-Type: application/json' \
  -d '{"inlet_pressure":0.11,"base_inlet_pressure":0.10,"design_flow":2.0,
       "du_min":0.85,"tail_drop_limit":0.10,"low_flow_ratio":0.70}'
curl -X DELETE http://localhost:8000/api/drip/samples/3

# 5) 导出 JSON（单轮完整结果，或按筛选导出列表）
curl -OJ 'http://localhost:8000/api/drip/export?round_id=1'
curl -OJ 'http://localhost:8000/api/drip/export?valve_id=2&date=2026-09-27'
```

首次启动内置两轮巡检示例（均挂在「中部干管」）：**秋茬滴灌首轮巡检**（黄瓜区支管压损、草莓区合格但 H08 滴头疑似堵塞、叶菜区入口压力偏低但仅 1/3 不触发共享异常）与**供压异常复核巡检**（3/3 分区供压偏低且 DU 合格，整轮判共享供压异常）。

### 复核示例（DU / 支管压损 / 共享供压 / 堵塞）

沿用 Docker Compose 启动（`docker compose up --build -d`），用内置两轮示例复核：

```bash
# 1) 最近一轮默认选中：应为 shared_pressure，命中 3 区、只产生 1 条共享维护项
curl -s http://localhost:8000/api/drip/state | python3 -c '
import sys, json
r = json.load(sys.stdin)["selected"]["result"]
print(r["verdict_label"], "|", r["rule"])
assert r["verdict"] == "shared_pressure" and r["shared_hit_count"] == 3
assert [m["type"] for m in r["maintenance"]] == ["shared_pressure"]
for z in r["zones"]:
    print(f"  {z[\"zone_name\"]}: DU={z[\"du\"]} 压力={z[\"inlet_pressure\"]}/{z[\"base_inlet_pressure\"]}"
          f" 样本={z[\"sample_count\"]} -> {z[\"verdict_label\"]}")
'

# 2) 首轮巡检：黄瓜区支管压损（DU≈0.6869、尾首降幅 42.2%）、草莓区 H08 疑似堵塞、
#    叶菜区入口压力偏低；1/3 未超过半数，不触发共享供压异常
RID=$(curl -s http://localhost:8000/api/drip/state | python3 -c '
import sys, json
for r in json.load(sys.stdin)["rounds"]:
    if "首轮" in r["name"]: print(r["id"])')
curl -s "http://localhost:8000/api/drip/state?round_id=$RID" | python3 -c '
import sys, json
r = json.load(sys.stdin)["selected"]["result"]
assert r["verdict"] == "normal", r["verdict"]
bz = {z["zone_name"]: z for z in r["zones"]}
assert bz["黄瓜区"]["verdict"] == "pipe_loss" and bz["黄瓜区"]["du"] == 0.6869
assert bz["黄瓜区"]["low_quarter_count"] == 5          # ceil(17/4)
assert bz["草莓区"]["verdict"] == "ok"
assert bz["叶菜区"]["verdict"] == "pressure_low"
types = sorted(m["type"] for m in r["maintenance"])
assert types == ["clog", "pipe_loss", "pressure"], types
clog = next(m for m in r["maintenance"] if m["type"] == "clog")
assert clog["position"] == "head" and clog["label"] == "H08"
print("首轮复核通过：", types, "| 堵塞点", clog["basis"])
'

# 3) 补录 / 删除样本后整轮重算：新建一轮挂在「北侧干管 · 番茄区」，
#    先录 7 个样本应为「样本不足、DU 不计算」，补到第 8 个后出现 DU
curl -s http://localhost:8000/api/state | python3 -c '
import sys, json
s = json.load(sys.stdin)
v = next(x["id"] for x in s["valves"] if x["name"] == "北侧干管")
z = next(x["id"] for x in s["zones"] if x["name"] == "番茄区")
print(v, z)' > /tmp/vz.txt
read V Z < /tmp/vz.txt
RID2=$(curl -s -X POST http://localhost:8000/api/drip/rounds -H "Content-Type: application/json" \
  -d "{\"name\":\"复核巡检\",\"valve_id\":$V,\"inspected\":\"2026-09-27T10:00\",\"zones\":[\
{\"zone_id\":$Z,\"inlet_pressure\":0.12,\"base_inlet_pressure\":0.10,\"design_flow\":2.0,\
\"du_min\":0.80,\"tail_drop_limit\":0.10,\"low_flow_ratio\":0.70}]}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["selected_id"])')
DZ=$(curl -s "http://localhost:8000/api/drip/state?round_id=$RID2" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["selected"]["result"]["zones"][0]["drip_zone_id"])')
for i in 01 02 03 04 05 06 07; do
  curl -s -o /dev/null -X POST "http://localhost:8000/api/drip/zones/$DZ/samples" \
    -H 'Content-Type: application/json' -d "{\"position\":\"head\",\"label\":\"H$i\",\"flow\":2.0}"
done
curl -s "http://localhost:8000/api/drip/state?round_id=$RID2" | python3 -c '
import sys, json
z = json.load(sys.stdin)["selected"]["result"]["zones"][0]
assert z["verdict"] == "insufficient" and z["du"] is None and z["sample_count"] == 7
print("7 个样本：样本不足，DU 不计算")'
curl -s -o /dev/null -X POST "http://localhost:8000/api/drip/zones/$DZ/samples" \
  -H 'Content-Type: application/json' -d '{"position":"head","label":"H08","flow":2.0}'
curl -s "http://localhost:8000/api/drip/state?round_id=$RID2" | python3 -c '
import sys, json
z = json.load(sys.stdin)["selected"]["result"]["zones"][0]
assert z["sample_count"] == 8 and z["low_quarter_count"] == 2 and z["du"] == 1.0
print("8 个样本：ceil(8/4)=2，DU=1.0 合格")'

# 4) 同段编号重复 / 非法位置 / 跨阀门分区均应 400
curl -s -o /dev/null -w '%{http_code}\n' -X POST "http://localhost:8000/api/drip/zones/$DZ/samples" \
  -H 'Content-Type: application/json' -d '{"position":"head","label":"H01","flow":1.0}'   # 400
curl -s -o /dev/null -w '%{http_code}\n' -X POST "http://localhost:8000/api/drip/zones/$DZ/samples" \
  -H 'Content-Type: application/json' -d '{"position":"end","label":"X1","flow":1.0}'    # 400

# 5) 删除保护：有巡检记录的分区 / 阀门不能直接删（先删轮次）
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE "http://localhost:8000/api/zones/2"   # 400
curl -OJ "http://localhost:8000/api/drip/export?round_id=$RID2"
```

页面侧复核：进入 `/drip`，默认选中「供压异常复核巡检」，顶部红色徽标「共享供压异常」并给出「3/3 超过半数」规则原文，维护清单只有一条 🚨 泵房 / 干管供压核查；从列表选「秋茬滴灌首轮巡检」，黄瓜区卡片为红色「支管压损」（展开可见 DU 0.6869、首/尾中位数与 42.2% 尾首降幅），草莓区合格且列出红色 H08 堵塞芯片与 🔧 维护项，叶菜区黄色「入口压力偏低」；左上方按「中部干管」筛选只列这两轮，改巡检日期可进一步过滤；在样本明细里删掉 H08 后维护清单同步少一条堵塞项，给某分区补录到 8 个样本后 DU 立即出现，全部为整轮重算结果。

## 肥料母液分罐（/fertilizer）

复用轮灌台的**温室分区**，为每个施肥配方把母液肥料分到 **A / B 两个母液罐**（钙盐与磷酸盐 / 硫酸盐通常不能同罐），录入用量与不相容关系后自动重算，支持保存与 JSON 导出。

### 数据模型（SQLite）

- **母液配方（fertilizer_recipes）**：名称 + **所属温室分区** + **A 罐容量 / B 罐容量（mL，正整数）** + 备注。分区被配方引用时禁止直接删除分区（返回 400，先删配方）。
- **肥料（fertilizer_items）**：配方内**名称唯一**，含**所需母液量（mL，非负整数）**；**用量为 0 的肥料不参与求解**（不进不相容图、不占罐），但仍保留其登记数据。
- **不相容关系（fertilizer_pairs）**：两种肥料的无向对（落库统一为小编号在前，唯一索引防重复），表示二者**必须分到不同罐**；删除任一肥料自动清理其关系。

### 求解规则（每次改用量 / 关系 / 容量后整体重算）

1. **只取用量 > 0 的肥料**构建无向不相容图，按边拆分**连通分量**并 BFS **二着色**（每个分量得到侧 0 / 侧 1 两组）。
2. **奇环冲突（conflict）**：任一连通分量无法二着色（含奇数环）时，对该分量做**奇偶状态 BFS**（每个节点带「路径奇偶性」状态）直接求**最短奇环 odd girth**，在全图各连通分量中取最短者；**等长环按规范化肥料 ID 序列**（最小编号起、唯一方向）稳定决胜。不再用「删 BFS 冲突边取端点普通最短路」的做法——该做法遇到冲突边的最短路与边闭合成更短偶环时会跳过该边，所有冲突边都如此时会漏报真正更长的奇环、误判可分配。环上各边高亮，**不再给分罐结果**；解除环上任意一条关系后冲突消失。
3. **全局翻转组合枚举**：每个分量有两种翻转方向（侧0→A/侧1→B，或反过来），枚举全部 2^k 种组合（分量超过 20 个时组合数过大，明确报错请用户拆配方）。
4. **决胜顺序**：① 双罐均不超容量的组合优先（可行优先）；② 可行组合中 **|A 体积 − B 体积| 最小**；③ 体积差相同时按 **A 罐肥料 ID 升序序列**字典序决胜。
5. **容量无解（overflow）**：所有组合都超容量时**绝不只显示“无法分配”**——返回**总超额最小**的组合（超额相同时继续按体积差、A 罐 ID 序列决胜），给出该方案的 **A/B 分罐明细、各自占用 / 超额量与剩余量**。
6. 组合数 ≤ 128 时，页面可展开**全部组合表**（是否可行、A/B 占用、体积差、总超额、A 罐 ID 序列），便于人工复核决胜过程。

### 页面 / 导出

- 左侧：配方历史列表（状态点、A/B 容量、参与肥料数、占用量）+ 新建 / 编辑配方（分区、A/B 罐容量），**肥料录入**（名称 + 用量，0 即暂不参与）与**不相容关系登记**（两个肥料下拉）。
- 右侧：总览徽标（✅ 可分配 / 🟠 最小超额方案 / 🔴 奇环冲突 / 📝 空），统计 A/B 占用与体积差；**🔴 冲突链**把最短奇环按 `肥料 ✕ 肥料 ✕ … ✕ 闭环` 展示；**分罐清单**两栏展示 A/B 罐肥料、占用量进度条、容量 / 剩余 / 超额；**连通分量**卡展示每分量两侧着色；**肥料明细**表可就地改用量（失焦即保存重算）、删除肥料；底部为不相容关系 chips（环上的红色高亮，可删除）。
- **编辑用量、不相容关系或 A/B 罐容量后立即重算**（所有写接口返回重算后的完整状态）；顶部 **⬇ 导出 JSON** 导出当前配方（含全部输入、连通分量、冲突环或全部翻转组合、最终分罐结果），按分区筛选时导出该分区配方列表。

### 操作示例（API）

```bash
# 1) 新建配方（挂在分区 2 黄瓜区；A/B 罐各 1000 mL）
curl -X POST http://localhost:8000/api/fertilizer/recipes \
  -H 'Content-Type: application/json' \
  -d '{"name":"复核配方","zone_id":2,"cap_a":1000,"cap_b":1000}'

# 2) 录入肥料（用量 mL；0 表示暂不参与求解）
curl -X POST http://localhost:8000/api/fertilizer/recipes/1/items \
  -H 'Content-Type: application/json' -d '{"name":"硝酸钙","amount":300}'
curl -X POST http://localhost:8000/api/fertilizer/recipes/1/items \
  -H 'Content-Type: application/json' -d '{"name":"硝酸钾","amount":400}'
curl -X POST http://localhost:8000/api/fertilizer/recipes/1/items \
  -H 'Content-Type: application/json' -d '{"name":"硫酸镁","amount":300}'

# 3) 登记不相容（钙盐不能与硫酸盐 / 大量钾盐同罐）
curl -X POST http://localhost:8000/api/fertilizer/recipes/1/pairs \
  -H 'Content-Type: application/json' -d '{"a_id":1,"b_id":2}'
curl -X POST http://localhost:8000/api/fertilizer/recipes/1/pairs \
  -H 'Content-Type: application/json' -d '{"a_id":2,"b_id":3}'

# 4) 查看重算结果：连通分量 / 翻转组合 / A、B 分罐清单
curl 'http://localhost:8000/api/fertilizer/state?recipe_id=1'

# 5) 改用量 / 关系 / 容量 -> 立即重算
curl -X PUT http://localhost:8000/api/fertilizer/items/1 \
  -H 'Content-Type: application/json' -d '{"amount":0}'          # 硝酸钙改为 0：不参与
curl -X PUT http://localhost:8000/api/fertilizer/recipes/1 \
  -H 'Content-Type: application/json' \
  -d '{"name":"复核配方","zone_id":2,"cap_a":400,"cap_b":400}'   # 容量不够 -> overflow

# 6) 删除关系 / 肥料 / 配方
curl -X DELETE http://localhost:8000/api/fertilizer/pairs/1
curl -X DELETE http://localhost:8000/api/fertilizer/items/3
curl -X DELETE http://localhost:8000/api/fertilizer/recipes/1

# 7) 导出 JSON
curl -OJ 'http://localhost:8000/api/fertilizer/export?recipe_id=1'
```

首次启动内置一份「越冬番茄母液配方」（挂在番茄区，A/B 罐各 2000 mL，共 5 种肥料、3 条不相容关系），不相容图为一条链加一个孤立点，最优解恰好 **A=B=1500 mL（体积差 0）**：A 罐硝酸钙 + 硫酸镁，B 罐硝酸钾 + 磷酸二氢钾 + 微量元素。

### 复核示例（二着色 / 奇环 / 最小超额 / 决胜）

沿用 Docker Compose 启动（`docker compose up --build -d`），用内置配方（默认 id=1）复核：

```bash
# 1) 内置配方：可分配、双罐 1500/1500、体积差 0；A 罐 ID 序列 [1,3]
curl -s 'http://localhost:8000/api/fertilizer/state?recipe_id=1' | python3 -c '
import sys, json
r = json.load(sys.stdin)["selected"]["result"]
c = r["decision"]["chosen"]
print(r["status"], "A:", c["used_a"], "B:", c["used_b"], "差:", c["diff"])
print("A 罐:", [(f["id"], f["name"]) for f in c["tank_a"]])
print("B 罐:", [(f["id"], f["name"]) for f in c["tank_b"]])
assert r["status"] == "ok" and c["used_a"] == 1500 and c["used_b"] == 1500 and c["diff"] == 0
assert c["a_ids"] == [1, 3]
'

# 2) 加一条硝酸钙(1)✕硫酸镁(3) 制造奇环：返回最短冲突环 1-2-3，不给分罐
curl -s -X POST http://localhost:8000/api/fertilizer/recipes/1/pairs \
  -H 'Content-Type: application/json' -d '{"a_id":1,"b_id":3}' | python3 -c '
import sys, json
r = json.load(sys.stdin)["selected"]["result"]
ring = [n["id"] for n in r["conflict_ring"]]
print(r["status"], "最短冲突环:", ring)
assert r["status"] == "conflict" and ring == [1, 2, 3] and r["decision"] is None
assert all(e["in_ring"] for e in r["edges"] if {e["a_id"], e["b_id"]} <= {1, 2, 3})
'
# 重复登记同一条关系（反向）应 400
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8000/api/fertilizer/recipes/1/pairs \
  -H 'Content-Type: application/json' -d '{"a_id":3,"b_id":1}'   # 400

# 3) 删除刚加的关系恢复可分配，再把 A/B 容量都改成 1000 -> 容量无解，
#    但返回最小总超额方案（1500/1500，A 超 500 + B 超 500 = 1000），不是“无法分配”
PID=$(curl -s http://localhost:8000/api/fertilizer/state | python3 -c '
import sys, json
for p in json.load(sys.stdin)["selected"]["pairs"]:
    if {p["a_id"], p["b_id"]} == {1, 3}: print(p["id"])')
curl -s -X DELETE "http://localhost:8000/api/fertilizer/pairs/$PID" > /dev/null
curl -s -X PUT http://localhost:8000/api/fertilizer/recipes/1 \
  -H 'Content-Type: application/json' \
  -d '{"name":"越冬番茄母液配方","zone_id":1,"cap_a":1000,"cap_b":1000,"note":""}' | python3 -c '
import sys, json
r = json.load(sys.stdin)["selected"]["result"]
c = r["decision"]["chosen"]
print(r["status"], "A/B:", c["used_a"], c["used_b"], "最小总超额:", c["excess_total"])
assert r["status"] == "overflow" and c["excess_total"] == 1000
assert c["tank_a"] and c["tank_b"]   # 仍给出完整分罐明细
'

# 4) 把某肥料用量改成 0：立即变为不参与（不进图、不占罐）；非正整数用量 400
curl -s -o /dev/null -w '%{http_code}\n' -X PUT http://localhost:8000/api/fertilizer/items/5 \
  -H 'Content-Type: application/json' -d '{"amount":1.5}'          # 400

# 5) 导出 JSON（含分量、冲突环或组合枚举、最终分罐）
curl -OJ 'http://localhost:8000/api/fertilizer/export?recipe_id=1'
```

页面侧复核：进入 `/fertilizer`，默认选中「越冬番茄母液配方」，绿色徽标「可分配」且 A/B 两栏各显示 1500/2000 mL、剩余 500 mL；展开「查看全部 4 种翻转组合」可见前两行均可行、体积差 0，按 A 罐 ID 序列选中 `[1, 3]` 行。在左下「两两不相容关系」选「硝酸钙 ✕ 硫酸镁」提交，页面立即变红：顶部「不相容冲突（奇环）」、出现 `#1 硝酸钙 ✕ #2 硝酸钾 ✕ #3 硫酸镁 ✕ #1（闭环）` 冲突链，分罐清单提示存在奇环暂不分罐。删除该条关系后恢复；把左上方 A/B 罐容量都改为 1000 保存，徽标变黄「容量超额（已给出最小超额方案）」，两罐进度条变红、显示「超额 500 mL」，而不是空白或“无法分配”。在肥料明细表把「微量元素」用量改为 0 并失焦，该肥料立即移入「不参与 / 待定」、分量与组合数同步减少。

### 复核示例（偶环捷径遮蔽奇环的漏报回归）

历史缺陷：旧实现只在 BFS 二着色的冲突边上去掉该边、取两端的**普通最短路**拼环；当该最短路与冲突边闭合成的是更短的**偶环捷径**时这条边被跳过，若每条冲突边都如此，真正更长的奇环会被整体漏报——接口错误返回「可分配」，甚至把不相容肥料放进同一罐。现在改为对每个连通分量做**奇偶状态 BFS**（节点状态附带路径奇偶性），直接求最短奇环（odd girth），等长环按规范化肥料 ID 序列决胜，`conflict_ring` / `conflict_component` / 环边标记三者一致。

沿用 Docker Compose 启动（`docker compose up --build -d`）。先跑纯模块回归（同时覆盖偶环捷径与更长奇环，不需要容器）：

```bash
# 宿主机有 Python 3.10+ 即可（只用标准库）
python3 -m unittest test_fertilizer_split -v
# 期望：Ran 17 tests ... OK
# 容器内也可执行：docker compose exec irrigation python3 -m unittest test_fertilizer_split -v
```

接口侧复核：下图含奇环 F1-F2-F3-F4-F5-F1（5 环），另有 3 条边的奇长捷径 F3-F6-F7-F4，与冲突边 F3-F4 / F6-F7 都闭合成 4-偶环（旧实现正是被它欺骗）。先恢复示例数据再建一份专用配方（7 种肥料 + 8 条关系；item id 由接口按名称解析，不写死编号）：

```bash
curl -s -X POST http://localhost:8000/api/reset -H 'Content-Type: application/json' \
  -d '{"sample":true}' > /dev/null

# 新建配方并录入 7 种肥料（每种 100 mL，A/B 罐各 1000 mL）
curl -s -X POST http://localhost:8000/api/fertilizer/recipes \
  -H 'Content-Type: application/json' \
  -d '{"name":"奇环回归配方","zone_id":1,"cap_a":1000,"cap_b":1000}' > /dev/null
RID=$(curl -s http://localhost:8000/api/fertilizer/state | python3 -c '
import sys, json
for r in json.load(sys.stdin)["recipes"]:
    if r["name"] == "奇环回归配方": print(r["id"])')
for n in F1 F2 F3 F4 F5 F6 F7; do
  curl -s -o /dev/null -X POST "http://localhost:8000/api/fertilizer/recipes/$RID/items" \
    -H 'Content-Type: application/json' -d "{\"name\":\"$n\",\"amount\":100}"
done
# 按名称取出 item id
eval "$(curl -s "http://localhost:8000/api/fertilizer/state?recipe_id=$RID" | python3 -c '
import sys, json
items = {x["name"]: x["id"] for x in json.load(sys.stdin)["selected"]["items"]}
for n in ["F1","F2","F3","F4","F5","F6","F7"]:
    print(f"ID_{n}={items[n]}")')"
# 5 环 F1-F2-F3-F4-F5-F1 + 偶环捷径 F3-F6-F7-F4
for pair in F1-F2 F2-F3 F3-F4 F4-F5 F5-F1 F3-F6 F6-F7 F7-F4; do
  x=${pair%-*}; y=${pair#*-}
  curl -s -o /dev/null -X POST "http://localhost:8000/api/fertilizer/recipes/$RID/pairs" \
    -H 'Content-Type: application/json' \
    -d "{\"a_id\":$(eval echo \$ID_$x),\"b_id\":$(eval echo \$ID_$y)}"
done

# 必须返回 conflict，最短冲突环为 5 环 F1..F5，且不给分罐结果
curl -s "http://localhost:8000/api/fertilizer/state?recipe_id=$RID" | python3 -c '
import sys, json
sel = json.load(sys.stdin)["selected"]
r = sel["result"]
names = [n["name"] for n in r["conflict_ring"]]
print(r["status"], "环:", names, "分量:", r["conflict_component"])
assert r["status"] == "conflict" and names == ["F1","F2","F3","F4","F5"]
assert r["decision"] is None and r["components"][0]["conflict"] is True
id2name = {x["id"]: x["name"] for x in sel["items"]}
marked = sorted((id2name[e["a_id"]], id2name[e["b_id"]]) for e in r["edges"] if e["in_ring"])
assert marked == [("F1","F2"),("F1","F5"),("F2","F3"),("F3","F4"),("F4","F5")], marked
print("环边标记:", ["%s-%s" % p for p in marked], "（F3-F6/F6-F7/F7-F4 三条捷径边不高亮）")
'

# 解除环上任意一条关系（如 F1-F2）后应立即恢复可分配，且任何不相容对不同罐
PID=$(curl -s "http://localhost:8000/api/fertilizer/state?recipe_id=$RID" | python3 -c '
import sys, json
sel = json.load(sys.stdin)["selected"]
id2name = {x["id"]: x["name"] for x in sel["items"]}
for p in sel["pairs"]:
    if {id2name[p["a_id"]], id2name[p["b_id"]]} == {"F1", "F2"}: print(p["id"])')
curl -s -X DELETE "http://localhost:8000/api/fertilizer/pairs/$PID" | python3 -c '
import sys, json
r = json.load(sys.stdin)["selected"]["result"]
assert r["status"] == "ok", r["status"]
c = r["decision"]["chosen"]
A, B = set(c["a_ids"]), set(c["b_ids"])
for e in r["edges"]:
    assert not ({e["a_id"], e["b_id"]} <= A) and not ({e["a_id"], e["b_id"]} <= B)
print("删边后恢复可分配：A", sorted(A), "B", sorted(B))
'
```

页面侧复核：打开 `/fertilizer` 选中「奇环回归配方」，顶部红色徽标「不相容冲突（奇环）」，冲突链为 `F1 ✕ F2 ✕ F3 ✕ F4 ✕ F5 ✕ F1（闭环）`；下方关系 chips 中只有环上 5 条关系红色高亮，`F3 ✕ F6 ✕ F7 ✕ F4` 三条捷径边不高亮；冲突期间分罐清单不指定罐位。删除环上任意一条关系后立即变回绿色「可分配」。可分配与容量超额两类结果的判定与展示保持不变。

## 执行核验（/verify）

灌完之后，把泵房 / 电磁阀日志按计划日期导入，系统把**实际运行**与**原计划**逐段对齐，回答四个问题：每个分区实际灌了多少、缺不缺水；实际运行有没有提前 / 延后 / 中断 / 越界；阀门并发与总流量有没有被突破；缺水的分区在**不改原计划**的前提下还能在哪些剩余时段补灌、补多少。

### 数据模型（SQLite，沿用现有数据卷）

- **核验批次（verify_batches）**：名称 + **计划日期（YYYY-MM-DD）** + 备注 + 创建时的计划修订号快照（核验始终按当前计划实时重算；计划后来被改过，页面顶部黄色提示「保存时修订 X，当前已到 Y」）。
- **运行事件（verify_events）**：`(batch_id, event_id)` 唯一；字段为 **event_id（业务唯一编号）、阀门、开始时间、结束时间、实际流量（L/min）**。时间为**本地时间、精确到分钟**，必须落在计划日期当天（结束允许次日 00:00 / 24:00）；删除批次级联删除其事件；被事件引用的阀门禁止删除（400，先删事件 / 批次）。

### 导入规则

- 批量接口 `POST /api/verify/batches/{id}/events`，请求体 `{"events":[...]}`（也兼容 `{"batch":[...]}`）。
- 事件必须**按开始时间递增**排列（允许同一开始时刻的并发事件，此时按 event_id 排序）；事件之间允许重叠、允许中断。
- **幂等**：同 `event_id` 且阀门 / 开始 / 结束 / 实际流量完全一致 -> 跳过，可安全重放整份日志；
- **冲突整批拒绝**：同 `event_id` 任一内容不同 -> 整个批次 400，不写入任何一条（先校验、后在单事务内插入）；
- 阀门不存在、结束不晚于开始、跨日期、带时区 / 秒精度、非数字流量同样整批 400 并指出第几条。

### 对齐与计量（左闭右开）

- 事件 `[start,end)` 与同阀门计划项 `[槽起,槽止)` 一律按**左闭右开**处理：恰好在计划开始时刻结束的事件不占该计划，恰好在计划结束时刻开始的事件不重叠。
- 对每个事件取其阀门当日全部计划项，在所有计划项端点处把事件切成连续子段：
  - 恰好压 **1 个**计划项 -> 归属该分区；
  - 一个都不压 -> **计划外运行（越界）**；
  - 同时压 **≥2 个**计划项 -> **证据不足**，列出候选分区，水量只登记、**不计入任何分区的已灌 / 缺水，绝不重复计量**。
- 计划外子段再判断与同阀门计划项是否**端点相接**：紧邻计划之前 = 该分区**提前运行**，紧邻之后 = **延后运行**（计入提前 / 延后分钟，水量仍记计划外）；夹在两个计划项之间标「两计划之间」，其余标「与计划不相邻」。
- 分区已灌水量按归属子段逐分钟求**并集**：重叠分钟取并集事件实际流量的最大值（并发 / 重复事件不重复计）；缺水 = max(0, 需水 − 已灌)，超灌 = max(0, 已灌 − 需水)；计划区间未被归属事件覆盖的分钟为**中断**分钟。
- **实际运行冲突**按分钟扫描全部事件区间：同分钟同阀门开启事件数 > 阀门并发上限 -> 阀门并发超限；同分钟全部事件实际流量之和 > 总流量上限 -> 总流量超限；相邻且同类型同事件集合的冲突自动合并展示。

### 补灌建议（不改原计划）

只对**已编排、阀门有效、仍缺水**的分区生成；候选槽 = 分区允许窗口内的 15 分钟槽 − 原计划槽 − 停水槽。按时间从早到晚贪心选取，每个候选槽同时满足：

1. 本分区按阀门**额定流量**加入后，叠加**原计划**占用总流量不超限、本阀门并发不超限；
2. 叠加**实际运行**占用同样不超限（建议不与已发生的运行打架）。

选中槽合并为连续建议时段。可行时给出具体起止时段与补入水量；**选不满时明确给出最小缺口**（仍缺多少 L、剩余允许窗口里还有多少可用槽），并把不可用槽按原因分组列出（停水时段 / 总流量超限（叠加原计划占用）/ 阀门并发超限（叠加实际运行占用）等）。

### 页面 / 导出

- 左侧：日期筛选 + 批次列表 + 新建批次（名称 / 计划日期 / 备注）、单条事件表单与**批量 JSON 导入**；右侧：总览徽标与统计（已灌 / 缺水 / 超灌 / 计划外 / 证据不足 / 冲突 / 剩余缺口）、实际运行冲突表、缺水分区补灌建议卡、分区核验台账（计划 / 实际时段、提前延后中断、各类水量）、证据不足与越界子段明细、事件台账（可逐条删除）。
- **补录 / 删除事件后全量重算**（计算模块无状态、结果不缓存）；批次保存于 SQLite，**⬇ 导出 JSON** 含批次元数据（保存时 / 当前计划修订）、全部子段归属、冲突、补灌建议与事件明细，文件名形如 `execution-verify-1-2026-10-01.json`。

### 操作示例（API）

```bash
# 0) 恢复示例（自动生成 1 个「示例核验批次」，覆盖全部五类结论）
curl -s -X POST http://localhost:8000/api/reset -H 'Content-Type: application/json' \
  -d '{"sample":true}' > /dev/null
curl -s http://localhost:8000/api/verify/state | python3 -c '
import sys, json
d = json.load(sys.stdin)
print("批次:", [(b["id"], b["name"], b["event_count"]) for b in d["batches"]])
r = d["selected"]["result"]
for z in r["zones"]:
    print(f"  {z[\"zone_name\"]}: {r[\"status_labels\"][z[\"status\"]]}"
          f" 已灌 {z[\"delivered_l\"]} 缺水 {z[\"deficit_l\"]}"
          f" 提前 {z[\"early_min\"]} 中断 {z[\"interrupt_min\"]} 计划外 {z[\"out_of_plan_l\"]}")
print("冲突:", [(c["type"], c.get("valve_name"), c["start"], c["end"]) for c in r["conflicts"]])
print("补灌:", [(m["zone_name"], m["feasible"], [(b["start"], b["end"]) for b in m["blocks"]])
                for m in r["makeup"]])
'

# 1) 手工新建一个当日批次
D=$(curl -s http://localhost:8000/api/state | python3 -c 'import sys,json;print(json.load(sys.stdin)["settings"]["plan_date"])')
BID=$(curl -s -X POST http://localhost:8000/api/verify/batches \
  -H 'Content-Type: application/json' \
  -d "{\"name\":\"班后复核\",\"plan_date\":\"$D\"}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["selected_id"])')

# 2) 按时间递增导入阀门事件（时间精确到分钟；同阀并发允许）
curl -s -X POST "http://localhost:8000/api/verify/batches/$BID/events" \
  -H 'Content-Type: application/json' -d '{
    "events": [
      {"event_id":"LOG-01","valve_id":1,"start":"'"$D"'T08:00","end":"'"$D"'T09:50","flow_lmin":25},
      {"event_id":"LOG-02","valve_id":1,"start":"'"$D"'T09:50","end":"'"$D"'T10:00","flow_lmin":25}
    ]}' | python3 -c 'import sys,json;d=json.load(sys.stdin);print("新增",d["imported"],"跳过",d["skipped"])'

# 3) 幂等：整份日志原样重放 -> imported=0, skipped=2，结果不变
#    冲突：把 LOG-01 的流量改成 99 再提交 -> 400，库内事件一条都不变
curl -s -o /dev/null -w '%{http_code}\n' -X POST "http://localhost:8000/api/verify/batches/$BID/events" \
  -H 'Content-Type: application/json' \
  -d '{"events":[{"event_id":"LOG-01","valve_id":1,"start":"'"$D"'T08:00","end":"'"$D"'T09:50","flow_lmin":99}]}'

# 4) 乱序 / 跨日期 / 秒精度 / 结束早于开始均整批 400
curl -s -o /dev/null -w '%{http_code}\n' -X POST "http://localhost:8000/api/verify/batches/$BID/events" \
  -H 'Content-Type: application/json' \
  -d '{"events":[{"event_id":"X2","valve_id":1,"start":"'"$D"'T10:00","end":"'"$D"'T10:30","flow_lmin":25},
                 {"event_id":"X1","valve_id":1,"start":"'"$D"'T09:00","end":"'"$D"'T09:30","flow_lmin":25}]}'

# 5) 删除单条事件 -> 全量重算；导出 JSON
EID=$(curl -s "http://localhost:8000/api/verify/state?batch_id=$BID" \
  | python3 -c 'import sys,json;print(next(e["id"] for e in json.load(sys.stdin)["selected"]["result"]["events"] if e["event_id"]=="LOG-02"))')
curl -s -X DELETE "http://localhost:8000/api/verify/events/$EID" > /dev/null
curl -s -OJ "http://localhost:8000/api/verify/export?batch_id=$BID" && ls execution-verify-*.json
```

### 复核示例（内置示例批次）

沿用 Docker Compose（`docker compose up --build -d`）。内置示例计划（贪心结果）为：草莓区 05:30–06:30（中部干管 25）、番茄区 08:00–10:00（北侧干管 25）、叶菜区 08:00–08:30、育苗区 08:00–08:45（苗床支管 10）、黄瓜区 18:00–19:30（中部干管 25）；停水 07:30–08:00，总流量上限 60 L/min。示例批次 7 条事件：

```bash
curl -s http://localhost:8000/api/verify/state | python3 -c '
import sys, json
d = json.load(sys.stdin)["selected"]["result"]

# 草莓 SV-STRAWBERRY-01 04:00–06:30：04:00–05:30 提前越界 2250 L，
# 计划内 05:30–06:30 足额 1500 L；台账 early_min=90
z = next(z for z in d["zones"] if z["zone_name"]=="草莓区")
assert z["delivered_l"] == 1500 and z["early_min"] == 90 and z["out_of_plan_l"] == 2250
print("草莓：提前 90 min，计划外", z["out_of_plan_l"], "L")

# 番茄 SV-TOMATO-01 08:00–09:50：中断 10 min，已灌 2750 / 缺水 250 L
z = next(z for z in d["zones"] if z["zone_name"]=="番茄区")
assert z["interrupt_min"] == 10 and z["deficit_l"] == 250
print("番茄：中断 10 min，缺水", z["deficit_l"], "L")

# 黄瓜 SV-CUCUMBER-01 17:50–19:30：17:50–18:00 计划外 250 L，提前 10 min
z = next(z for z in d["zones"] if z["zone_name"]=="黄瓜区")
assert z["early_min"] == 10 and z["out_of_plan_l"] == 250 and z["deficit_l"] == 0

# 冲突：08:10–08:16 总流量 100 > 60（番茄 25 + 叶菜 25 + 演示 40 + 育苗 10），
# 08:16–08:20 为 75 > 60（叶菜事件已停）；另含两条阀门并发超限
types = sorted({c["type"] for c in d["conflicts"]})
assert types == ["total_flow", "valve_concur"]
for c in d["conflicts"]:
    print("冲突:", c["type"], c.get("valve_name") or "全系统", c["start"], c["end"], c["event_ids"])

# 叶菜 08:00–08:16 运行（其中 08:10–08:16 与 40 L/min 演示事件并发，
# 并集按 40 计）：已灌 650 L，缺水 100 L；计划 30 min 中实际覆盖 20 min，
# 中断 10 min；补 1 槽即可，建议 08:45–09:00
z = next(z for z in d["zones"] if z["zone_name"]=="叶菜区")
assert z["deficit_l"] == 100 and z["interrupt_min"] == 10
assert z["delivered_l"] == 650
l = next(m for m in d["makeup"] if m["zone_name"]=="叶菜区")
assert l["feasible"] and l["blocks"][0]["start"] == "08:45"

# 补灌建议不改原计划：番茄 250 L -> 06:00–06:15（额定 25 L/min × 15 min）
t = next(m for m in d["makeup"] if m["zone_name"]=="番茄区")
assert t["feasible"] and t["blocks"][0]["start"] == "06:00" and t["remaining_l"] == 0
print("番茄补灌建议：", [(b["start"], b["end"]) for b in t["blocks"]])
print("全部建议：", [(m["zone_name"], m["feasible"], m["remaining_l"]) for m in d["makeup"]])
'
```

**无解最小缺口**复核（在上面的环境继续）：把番茄区允许窗口收窄到恰好等于其计划窗口并制造缺水，补灌建议返回 `feasible=false`、`free_slot_count=0` 与明确原因：

```bash
# 1) 番茄区允许时段改为 08:00–10:00（与计划完全相同，无剩余槽）
read TID VID < <(curl -s http://localhost:8000/api/state | python3 -c '
import sys, json
z = next(z for z in json.load(sys.stdin)["zones"] if z["name"]=="番茄区")
print(z["id"], z["valve_id"])')
curl -s -X PUT "http://localhost:8000/api/zones/$TID" -H 'Content-Type: application/json' \
  -d "{\"name\":\"番茄区\",\"water_l\":3000,\"valve_id\":$VID,\"earliest\":\"08:00\",\"latest\":\"10:00\"}" > /dev/null
# 2) 核验重算：番茄缺水 250 L 但剩余允许时段为 0 -> 最小缺口 = 250 L + 原因
curl -s http://localhost:8000/api/verify/state | python3 -c '
import sys, json
m = next(m for m in json.load(sys.stdin)["selected"]["result"]["makeup"] if m["zone_name"]=="番茄区")
print("可行:", m["feasible"], "| 最小缺口:", m["remaining_l"], "L |", m["reason"])
assert not m["feasible"] and m["remaining_l"] == 250 and m["free_slot_count"] == 0
'
# 3) 恢复示例
curl -s -X POST http://localhost:8000/api/reset -H 'Content-Type: application/json' -d '{"sample":true}' > /dev/null
```

页面侧复核：打开 `/verify`，默认选中「示例核验批次」，顶部徽标为「存在待处理问题」；右侧依次可见总流量 / 阀门并发冲突表（红色时段 + 涉及 event_id）、番茄 / 叶菜补灌建议卡（绿色建议时段芯片 + 剩余允许窗口）、分区台账（番茄缺 250、草莓提前 90 / 计划外 2250、黄瓜提前 10）、越界子段明细（草莓 04:00–05:30 标「提前运行」）与事件台账；在左侧删掉 `SV-DEMO-FLOW` 后冲突与叶菜计量立即全量重算；再原样提交该事件返回「幂等跳过」，把流量改为别的值则红色报错且整批不落库。

后端回归（无需容器，纯模块级；Flask 接口在容器 / 装好 Flask 的环境运行）：

```bash
python3 -m unittest test_execution_check -v
# 覆盖：时间/日期/时区校验、批内递增、幂等跳过、内容冲突整批拒绝、
#       唯一归属 / 提前延后 / 中间间隔 / 证据不足不重复计量、并集水量不重复计、
#       阀门并发与总流量冲突及区间合并、补灌可行 / 停水避让 / 计划并发占用 / 无解最小缺口
```

数据库迁移说明：新增 `verify_batches` / `verify_events` 两张表由建库脚本 `CREATE TABLE IF NOT EXISTS` 幂等创建；旧库首次启动经 `PRAGMA user_version=4` 迁移（表结构自动补齐、`plan_revision` 等既有数据不动），无需新增服务或数据卷，继续使用 SQLite 与现有 Docker Compose。

## 内置示例



| 分区 | 水量 | 阀门（流量） | 允许时段 |
|---|---|---|---|
| 番茄区 | 3000 L | 北侧干管 25 L/min | 06:00–10:00 |
| 黄瓜区 | 2250 L | 中部干管 25 L/min | 18:00–22:00 |
| 草莓区 | 1500 L | 中部干管 25 L/min | 05:30–08:30 |
| 叶菜区 | 750 L | 中部干管 25 L/min | 08:00–11:00 |
| 育苗区 | 450 L | 苗床支管 10 L/min | 07:00–09:00 |

总流量上限 60 L/min，停水 07:30–08:00。可以试试把「番茄区」拖到 07:30 附近或把总流量上限改小，观察冲突标注。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 获取全部数据与校验结果（含计划修订号 `revision`） |
| POST | `/api/generate` | 重新自动编排；可携带 `base_revision`，基线过期返回 409（不落库、无部分更新） |
| PUT | `/api/schedule` | 整表替换编排（拖拽/保存使用）；携带 `base_revision` 做乐观并发，过期返回 409 并附最新 `state` 供逐项合并；缺省该字段兼容旧客户端 |
| GET | `/api/export` | 导出 JSON 文件（含 `plan_revision`） |
| POST | `/api/settings` | 更新日期 / 总流量上限（总流量实际变化时递增计划修订） |
| POST/PUT/DELETE | `/api/valves[/{id}]` | 阀门增改删 |
| POST/PUT/DELETE | `/api/zones[/{id}]` | 分区增改删 |
| POST/PUT/DELETE | `/api/blackouts[/{id}]` | 停水时段增改删 |
| POST | `/api/reset` | 清空（`{"sample":false}`）或恢复示例（`{"sample":true}`） |
| GET | `/water` | 水质监测页面 |
| GET | `/api/water/state` | 水质状态；参数 `source_id`、`date`（YYYY-MM-DD）、`limit`（默认 50，0=全部） |
| GET | `/api/water/export` | 按 `source_id`/`date` 筛选导出 JSON 文件 |
| POST/PUT/DELETE | `/api/water/sources[/{id}]` | 水源增改删（删除级联其区间与采样） |
| PUT | `/api/water/sources/{id}/ranges` | 配置四项指标正常区间并重算状态 |
| POST | `/api/water/sources/{id}/samples` | 录入采样；支持单条对象或 `{"batch":[...]}`（乱序/缺项/非法单位整批拒绝） |
| DELETE | `/api/water/samples/{id}` | 删除单条采样并重算该水源状态 |
| GET | `/disease` | 病害接触追踪页面 |
| GET | `/api/disease/state` | 案件/工具/事件及重放结果；参数 `case_id`（默认最近案件） |
| GET | `/api/disease/export` | 导出当前案件 JSON（已冻结案件导出冻结快照） |
| POST/PUT/DELETE | `/api/disease/tools[/{id}]` | 可复用工具增改删（开放案件引用中的工具禁止删除 400；冻结案件引用中的工具可删，历史事件保留编号+名称） |
| POST/PUT/DELETE | `/api/disease/cases[/{id}]` | 案件增改删；PUT 可改病害名称 / 潜伏时长（冻结案件只读，返回 409） |
| POST | `/api/disease/cases/{id}/events` | 登记确诊 / 到访 / 消毒事件并重算 |
| PUT/DELETE | `/api/disease/events/{id}` | 修改 / 删除单条事件并重算 |
| POST | `/api/disease/cases/{id}/freeze` | 确认冻结，保存案件快照 |
| GET | `/heat` | 作物积温预测页面 |
| GET | `/api/heat/state` | 批次 / 分区及当前批次全量重算结果；参数 `batch_id`、`zone_id`（按分区筛选） |
| GET | `/api/heat/export` | 按 `batch_id` / `zone_id` 导出 JSON 文件（含日台账、缺口、首次达标、外推依据） |
| POST/PUT/DELETE | `/api/heat/batches[/{id}]` | 积温批次增改删（参数保存 / 删除批次后全量重算；分区有批次时删除分区返回 400） |
| POST | `/api/heat/batches/{id}/readings` | 录入温度读数；单条对象或 `{"batch":[...]}`，支持补录（同批次时间戳唯一） |
| DELETE | `/api/heat/readings/{id}` | 删除单条读数并按时间全量重算 |
| GET | `/drip` | 滴灌均匀度巡检页面 |
| GET | `/api/drip/state` | 巡检轮次 / 阀门 / 分区及选中轮次整轮重算结果；参数 `round_id`、`valve_id`、`date`（YYYY-MM-DD） |
| GET | `/api/drip/export` | 按 `round_id` 导出单轮完整结果，或按 `valve_id`/`date` 导出轮次列表 JSON |
| POST/PUT/DELETE | `/api/drip/rounds[/{id}]` | 巡检轮次增改删（一轮一条阀门；PUT 替换其分区参数；删除级联分区行与样本；阀门 / 分区有巡检记录时禁止删除，返回 400） |
| PUT | `/api/drip/zones/{id}` | 修改单分区入口压力与五项巡检参数，保存后整轮重算 |
| POST | `/api/drip/zones/{id}/samples` | 录入滴头样本（首 head / 中 middle / 尾 tail + 编号 + 流量）；单条或 `{"batch":[...]}`，同分区同段编号唯一 |
| DELETE | `/api/drip/samples/{id}` | 删除单个滴头样本并整轮重算 |
| GET | `/fertilizer` | 肥料母液分罐页面 |
| GET | `/api/fertilizer/state` | 配方 / 分区及选中配方整体重算结果；参数 `recipe_id`、`zone_id`（按分区筛选） |
| GET | `/api/fertilizer/export` | 按 `recipe_id` 导出单份配方（含分量 / 冲突环 / 组合枚举 / 分罐结果），或按 `zone_id` 导出列表 JSON |
| POST/PUT/DELETE | `/api/fertilizer/recipes[/{id}]` | 母液配方增改删（名称 + 分区 + A/B 罐容量；保存容量立即重算；删除级联肥料与关系） |
| POST | `/api/fertilizer/recipes/{id}/items` | 添加肥料（名称 + 所需母液量 mL，非负整数；0 不参与求解） |
| PUT/DELETE | `/api/fertilizer/items/{id}` | 修改肥料名称 / 用量（改用量立即重算）或删除（自动清理其不相容关系） |
| POST | `/api/fertilizer/recipes/{id}/pairs` | 登记两种肥料的两两不相容关系（无向、不可重复、不可自指） |
| DELETE | `/api/fertilizer/pairs/{id}` | 删除一条不相容关系并立即重算 |
| GET | `/verify` | 执行核验页面 |
| GET | `/api/verify/state` | 核验批次 / 阀门及选中批次全量重算结果；参数 `batch_id`、`date`（计划日期筛选） |
| GET | `/api/verify/export?batch_id=` | 导出单个核验批次 JSON（批次元数据 + 对齐明细 + 冲突 + 补灌建议 + 事件明细） |
| POST | `/api/verify/batches` | 新建核验批次（名称 + 计划日期 YYYY-MM-DD + 备注，记录当时计划修订号） |
| DELETE | `/api/verify/batches/{id}` | 删除批次及其全部事件 |
| POST | `/api/verify/batches/{id}/events` | 按时间递增导入运行事件（`{"events":[...]}` 或 `{"batch":[...]}`）；同编号同内容幂等、内容冲突 / 乱序 / 非法整批 400 |
| DELETE | `/api/verify/events/{id}` | 删除单条运行事件并全量重算 |

## 项目结构

```
├── app.py              # Flask 路由 + SQLite 持久化（轮灌 + 水质 + 病害 + 积温 + 滴灌 + 母液分罐 + 执行核验）
├── scheduler.py        # 轮灌编排与校验引擎（15 分钟槽）
├── execution_check.py  # 执行核验：半开区间切分归属 / 证据不足不重复计量 / 并发·总流量冲突扫描 / 剩余窗口补灌建议 / 幂等导入校验
├── water_quality.py    # 水质指标校验与「连续两次」状态机
├── disease_trace.py    # 病害接触追踪：确诊/到访/消毒按时间重放引擎
├── crop_heat.py        # 作物积温：温度截断 / 梯形积分 / 午夜拆分 / 缺口登记 / 7 完整日外推
├── drip_uniform.py     # 滴灌巡检：低四分位 DU / 共享供压异常 / 支管压损 / 滴头堵塞判定
├── fertilizer_split.py # 母液分罐：不相容图连通分量二着色 / 奇偶状态 BFS 最短奇环 / 全局翻转组合寻优 / 最小超额方案
├── test_execution_check.py # 执行核验回归（半开切分归属 / 证据不足不重复计量 / 冲突扫描 / 补灌最小缺口 / 幂等与整批拒绝）
├── test_fertilizer_split.py # 母液分罐回归测试（偶环捷径漏报、更长奇环、同长环规范化决胜、ok/overflow/empty/error）
├── test_schedule_revision.py # 轮灌计划防覆盖修订回归（CAS 先提交者胜出、409 无部分更新、合并期间再变、约束变更失效基线、旧客户端兼容）
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── data/               # SQLite 数据库（运行时生成）
└── static/
    ├── index.html      # 轮灌台页面结构
    ├── app.js          # 轮灌台渲染 / 表单 / 拖拽 / 即时校验 / 修订基线提交 / 冲突草稿与逐项合并
    ├── water.html      # 水质监测页面结构
    ├── water.js        # 水质录入 / 筛选 / 趋势图 / 导出
    ├── disease.html    # 病害追踪页面结构
    ├── disease.js      # 案件 / 工具 / 事件登记 / 重放结果 / 筛选 / 导出
    ├── heat.html       # 积温预测页面结构
    ├── heat.js         # 批次 / 读数录入 / 曲线 / 日台账 / 缺口 / 外推 / 导出
    ├── drip.html       # 滴灌巡检页面结构
    ├── drip.js         # 轮次 / 参数 / 首中尾样本录入 / 判定卡 / 维护清单 / 筛选 / 导出
    ├── fertilizer.html # 母液分罐页面结构
    ├── fertilizer.js   # 配方 / 肥料用量 / 不相容关系登记 / 冲突链 / 分罐清单 / 组合枚举 / 导出
    ├── verify.html      # 执行核验页面结构
    ├── verify.js        # 批次 / 事件单条与批量导入 / 冲突 / 补灌 / 台账 / 子段明细 / 删除重算 / 导出
    └── style.css       # 时间轴等样式（water.css、disease.css、heat.css、drip.css、fertilizer.css、verify.css 为对应页面补充样式）
```
