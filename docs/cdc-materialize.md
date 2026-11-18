# cdc-materialize 数据契约 · 交错事务日志的提交可见快照

`cdc-materialize` 把带显式事务边界的多表交错行变更日志（CDC log）物化为最终
**已提交可见**关系，并为每笔事务输出接纳/拒绝证据。仅用 Python 标准库，不联网；
复用 `common.read/save` 的 8MB 输入限制、源字节 SHA-256 与原子发布。

## 1. 入口

```sh
loan-schedule cdc-materialize --source INPUT --output REPORT
python -m loan_schedule.cli cdc-materialize --source INPUT --output REPORT
cdc-materialize --source INPUT --output REPORT      # 专用 console entry point
```

三个入口参数与语义完全相同（专用入口固定派发 `cdc-materialize` 子命令）。
输入与输出同路径、输出为符号链接一律拒绝；输入只读。结构/事务框架错误、任何
解析或物化内部异常、结果核验失败、写入失败均退出 2 并保留旧报告；只有合法日志内
的业务提交拒绝属于成功报告内容，退出 0。

## 2. 输入字段（顶层恰为四个键）

```json
{"dataset_id": "ident", "tables": [...], "initial": [...], "records": [...]}
```

未知字段、缺失字段、重复 JSON 键、非有限数（含 `NaN`/`Infinity` 记号与溢出为
`Infinity` 的数值字面量如 `1e999`）均为整份输入错误。

### 2.1 标识符与标量

- 所有 `dataset_id`、`table`、列名、`txid`、`fk_id` 沿用 `common.ident`：非空白
  字符串、无首尾空白、≤128 字符、无控制字符。
- integer：`type(x) is int`（拒绝 bool），范围 `[-10^12, 10^12]`。
- string：≤256 个 Unicode 码点，按原始码点保留，不做大小写、空白或任何归一化。
- boolean：仅 JSON `true`/`false`，不与整数互隐。
- null：仅在该列 `nullable=true` 时合法；`nullable` 本身必须是布尔。

### 2.2 tables（1..16 项，table 名唯一）

每项恰为 `{table, columns, primary_key, foreign_keys}`：

- `columns`：1..16 项 `{name,type,nullable}`，表内 name 唯一；type 仅
  `integer/string/boolean`。
- `primary_key`：1..4 个不重复列名的**有序**数组，列必须存在且 `nullable=false`。
- `foreign_keys`：0..16 项 `{fk_id,columns,ref_table}`，fk_id 表内唯一；columns
  为不重复列名有序数组；`ref_table` 必须引用已声明表（允许前向、循环与自引用，
  不要求声明顺序构成拓扑序）。columns 的长度与逐位置类型必须与目标表
  **声明的完整主键**完全对应；不接受任何其他候选引用键。
- FK 列本身可以为 `nullable=true`（见 2.5 的空值豁免）。

### 2.3 initial（0..2000 项）

每项恰为 `{table,values}`；`values` 是按该表 `columns` **声明顺序**排列的完整行
数组（宽度与逐列类型必须合法，声明顺序决定位置解释，不会重排）。

- 先装入全部初始行，再统一核主键重复；然后做**全表联合**外键检查，因此循环与
  自引用不会因输入顺序被误拒。
- 初始关系主键重复或外键悬空 → 整份输入失败。

### 2.4 records（0..10000 项，按数组顺序消费）

- `lsn`：1..10^12 的严格递增整数（拒绝 bool），不自动排序；任何非递增即失败。
- 框架记录 `begin/commit/abort` 恰为 `{lsn,txid,kind}`；`change` 恰为
  `{lsn,txid,kind:"change",table,before,after}`。
- `before/after` 各为 null 或完整合法行数组，不能同时为 null：
  `before=null` 插入、`after=null` 删除、二者皆有更新（允许改主键）。
- 任意 txid 全文件只能 begin 一次；change/commit/abort 必须对应当前 open 的事务，
  结束后不得再出现同 txid 记录。允许空提交、空 abort；EOF 仍 open 合法。
- aborted/open 事务的行数组仍须完成**静态**结构与类型校验，但其动态
  before 像、主键与外键合法性不在解析期重演。
- 未知 kind、悬空表/列引用、类型或范围错误均为整份输入错误。

## 3. 物化语义与错因优先级

### 3.1 事务装配

按严格递增的日志位置，把每个 txid 的 change 以原始次序暂存在该事务私有的有序
列表中；begin/change/commit/abort 可与其他事务任意交错。未结束事务与 abort
事务的行变更绝不进入可见关系。

### 3.2 提交时重演（不在 begin/change 到达时预校验）

每个 commit 到达时，在**此刻最新已接纳快照**上为该事务构建私有工作副本，从首条
change 开始按原序重演；事务的 before 像不缓存任何早期校验结论，其他事务提交后
旧结论自动失效。任一行冲突立即拒绝整笔事务，工作副本整体丢弃，不泄漏部分行、
不撤销其他已接纳事务、不污染其他未结束事务。业务拒绝不阻塞后续事务。

单条 change 的判定次序（同一变更可能产生多个错误时严格遵守）：

1. 插入：目标主键已存在 → `primary_key_conflict`。
2. 删除/更新：先按 before 的主键定位——找不到 → `missing_row`；找到但完整行
   不按**同类型同值**相等 → `before_mismatch`（`True≠1`、字符串不归一化）。
3. 更新：先删除旧行，再检查 after 主键是否与剩余行冲突 →
   `primary_key_conflict`（定位/目标主键用 after 键）。

### 3.3 外键：整笔重演结束后的全关系检查

只有全部 change 成功后才核外键，且对**全部候选关系**扫描，包括完全未改动的子表。
因此：

- 同一事务内可先插子后插父、先删父后删子；循环/自引用在单笔内成立。
- 删除父行时，未改动子表中的反向引用同样使整笔拒绝；无任何级联、补父、置空或
  自动修复。
- 任一 FK 分量为 null 即豁免该条引用。
- 多个悬空引用按 `(table, fk_id, 带类型主键元组)` 升序取首项，code 为
  `foreign_key`，`change_lsn=null`，`table/primary_key` 定位悬空**子行**，
  `fk_id` 指其约束。

拒绝原因先取该事务中最早失败的 change；全部 change 成功才核外键。后续每笔事务
仍在最新已接纳关系上独立重演，依赖被拒事务的后续事务不会被强行接纳。

### 3.4 类型化排序

最终行按声明主键元组升序；每个主键位置只有其声明类型，排序键按类型秩
（布尔 `false<true` < 整数 < 字符串）加值比较，字符串按 Unicode 码点。

## 4. 输出报告

顶层恰为 `{schema_version:1, dataset_id, source_sha256, tables, transactions}`，
JSON 键排序、UTF-8、原子替换，同源重跑字节一致。

- `tables` 按 table 名排序，逐表 `{table,columns,primary_key,foreign_keys,rows}`：
  columns 与 primary_key 保留声明次序；foreign_keys 按 fk_id 排序；rows 为最终
  完整行数组，按 3.4 的主键元组排序；空表也输出。
- `transactions` 按 begin 的 lsn 升序，逐项
  `{txid,begin_lsn,end_lsn,status,change_lsns,error}`：
  - `end_lsn` 为 commit/abort 位置，open 时为 null；
  - status ∈ `accepted/rejected/aborted/open`，空提交也是 accepted；
  - `change_lsns` 保留该事务全部 change 的 lsn（原序）；
  - 仅 rejected 的 error 非 null，恰为
    `{code,change_lsn,table,primary_key,fk_id}`；行冲突 code 为
    `missing_row/before_mismatch/primary_key_conflict`，change_lsn 指向失败
    change、fk_id=null；外键 code=`foreign_key`、change_lsn=null。其余状态
    error=null，aborted/open 无任何可见效果。
- 置换 tables/initial 的数组顺序不改变业务结果；records 重排不在等价输入范围内。

## 5. 候选关系表示的两种可行设计与正确性义务

两种设计都**必须**完整执行 3.2/3.3 的约束判定；“先让所有变更落库、事后不校验
约束”的写法不是备选方案，而是错误实现。

### 设计 A（本实现采用）：已接纳快照 dict + 每事务私有工作副本

已接纳关系按表保存 `{主键元组: 完整行}` 的 dict。commit 时对每张表做浅拷贝
（dict 复制，行元组不可变），在私有副本上重演；成功则整体替换已接纳关系，失败
直接丢弃副本。

- **隔离未提交行**：暂存 change 只存在事务私有列表，工作副本在 commit 前对其他
  事务不可见，天然满足交错隔离。
- **失败回滚**：回滚等于“不替换引用”，O(表数) 且不可能漏撤某一行；不存在半提交
  状态。
- **捕获未改动子表的反向引用**：外键检查遍历工作副本中的**全部表全部行**，未改动
  子表天然在扫描集合内。
- 代价：每次 commit 复制全量 dict（行数受 2000 初始 + 10000 记录约束，规模可控）。

### 设计 B（备选）：单主关系 + 事务影子页（delta map）+ 提交期合并

只维护一份主关系，每个 open 事务维护自己的 delta（插入/删除/更新影子页），读
自己的变更时叠加影子页；commit 时先在“主关系+本事务影子页”的逻辑视图上完成
同样的 before/主键/全关系外键判定，通过后才把 delta 合并进主关系。

- **隔离未提交行**：影子页按 txid 隔离，其他事务的视图不含未提交 delta；实现必须
  保证读路径始终先查本事务影子页再查主关系。
- **失败回滚**：回滚等于丢弃影子页；但合并阶段若中途异常会留下半合并状态，必须用
  “先在私有合并结果上判定、再一次性切换”的方式达到与 A 等价的原子性。
- **捕获未改动子表的反向引用**：外键检查不能只扫 delta 触及的行，必须以
  “主关系叠加影子页”为候选，对全部表（含未改动子表）逐行核对，否则删除父行的
  反向引用会漏检。
- 代价：读路径更复杂（影子页叠加、同键连续变更的折叠必须保留 change 原序），
  省掉全量 dict 复制；在本产品的规模下没有收益，因此选用设计 A。

两种设计共同的不可让步义务：①未提交/aborted 行绝不可见；②回滚不允许部分行
泄漏；③外键判定覆盖未改动子表并支持循环、自引用与空值豁免；④before 像以提交
时刻的最新已接纳关系为准。

## 6. 发布前核验

`cdc.validate_result` 在 `common.save` 前独立核验，任何不一致即失败、退出 2 且
旧报告字节不变：

1. 表集合与声明（列名/类型/nullable 次序、主键次序、外键 fk_id 次序）与输入一致；
2. 每行宽度、逐列类型与可空性合法，主键唯一且按类型化元组排序；
3. 对发布出的最终关系重新做全量外键裁决，不得存在悬空引用；
4. 审计框架（begin/end lsn、change_lsns、status 取值、error 键集合）逐条回溯到
   源 records；每个错误定位（change_lsn/table/primary_key/fk_id）与源记录一致；
5. 用**不调用生产重演与约束判定**的第二套独立重演（朴素 dict + 本地类型感知
   助手）重算全部审计结论与每张表最终行，逐项比对。

## 7. 边界与验收资产

- 不联网、无第三方依赖；单操作者批处理，不承诺并发发布或断电恢复。
- `fixtures/cdc/` 为原创虚构样例：联动验收、提交次序对照、循环外键、覆盖长廊
  （复合主外键、null 分量豁免、自引用、主键更新、同键连续变更、同事务删后再插、
  空事务、abort、EOF open、全部行冲突 code、未改动子表反向引用）。
- `tests/test_cdc.py` 内的朴素参考解释器不 import 生产重演/约束函数，对 120 组
  合成随机交错日志逐项比对全部审计状态、首个错误与最终全表内容；另对每个提交点
  做前缀审计稳定性检查，并覆盖 CLI 三入口、字节确定性、同路径/符号链接拒绝、
  失败保留旧报告与退出码。
