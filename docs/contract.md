# Equal-principal loan data schedule v2 · 静态计划与还款事实集市

本仓提供两个互不混淆的能力。**静态计划**沿用 v1 契约，只按合同生成“应还”计划；它不表示银行真实收到了钱。**还款事实集市**（SQLite 单贷款账本）记录真实收款、撤销与追溯更正，并在任意历史知识截止点重算实际债务。

## 1. 静态计划入口（v1 契约，未改变）

```sh
loan-schedule --source CONTRACT.json --output PLAN.json
```

合同字段固定为 `principal periods rate_numerator rate_denominator`：principal 为 1..10^12 整数货币最小单位；periods 为 1..10000 且不得超过 principal；rate_numerator 为 0..10^6、rate_denominator 为 1..10^9，表示每期利率。
前 periods-1 期偿还 `floor(principal/periods)`，末期偿还尾差；每期开期余额乘每期利率，以非负 half-up 逐期舍入至整数最小单位，保留舍入前有理数字符串。总本金恰等初始本金，末余额 0。
输出结构（`schema_version:1`）：`{schema_version, source_sha256, result:{schedule:[{period,opening,principal,exact_interest,interest,payment,closing}],total_principal,total_interest,total_payment,rounding}}`。输入/输出不得相同,输出符号链接拒绝，成功后原子替换，失败退出 2 并保留旧报告。

## 2. 还款事实集市

三个子命令，全部为本地文件操作，不访问网络：

```sh
loan-schedule ledger-init   --db PATH --source CONTRACT.json
loan-schedule ledger-import --db PATH --source BATCH.json
loan-schedule ledger-report --db PATH --known-seq K --through-period T --output REPORT.json
```

- `ledger-init`：以静态合同同四字段、同参数范围建立**不可变合同**。数据库已存在（含符号链接、零字节文件）即拒绝，退出 2，不覆盖。成功后 `seq` 从 0 开始（seq 0 是一个无源字节的“空知识版本”）。初始化先写入临时文件再一次性创建目标文件，失败不会留下半成品数据库。
- `ledger-import`：读入一个批次并原子提交；数据库不存在时报错退出 2，**不会隐式建库**。成功输出 `{schema_version:2,command:"ledger-import",batch_id,source_sha256,seq,event_count,duplicate}` 到 stdout。
- `ledger-report`：按 `known_seq` 与 `through_period` 发布报告；数据库不存在、参数越界、输出为符号链接或与数据库同路径均退出 2。成功按既有安全规则（临时文件 + `os.replace`）原子替换 JSON。

所有新 JSON 输入都经过与静态入口相同的受限读取器：≤8MB、拒绝重复键、拒绝非有限数值（`NaN`/`Infinity`）、拒绝用布尔冒充整数、拒绝未声明字段。输入文件只读，不被命令修改。

### 2.1 批次输入

```json
{"batch_id":"B1","events":[
  {"event_id":"receipt-1","rev":1,"op":"upsert","kind":"receipt","period":1,"amount":50},
  {"event_id":"reversal-1","rev":1,"op":"upsert","kind":"reversal","period":2,"target":"receipt-1"},
  {"event_id":"receipt-1","rev":2,"op":"void"}
]}
```

- 顶层恰为 `batch_id`、`events`；`events` 为数组，最多 20000 条，可为空。空数组仍是成功批次，照常占用一个 `knowledge_seq`。
- `batch_id` / `event_id` / `target` 沿用 `ident`：非空字符串、无首尾空白、≤128 字符、无控制字符。
- `op` 为 `upsert` 或 `void`。`upsert` 的 `kind` 为 `receipt`（字段 `event_id rev op kind period amount`）或 `reversal`（字段 `event_id rev op kind period target`）。`void` 仅含 `event_id rev op`，且不得带其他字段。
- `amount` 为 1..10^12 整数；`period` 在 1..合同期数内；`rev` 从 1 起连续递增，整数。
- 数组可乱序。**同一批次内每个 event_id 最多一个版本**；`rev` 必须是该事件**已提交最高 rev + 1**（节内合同时，同一批次多个事件同时生效，不按数组位置逐条判定）。首次版本不得为 `void`；事件一旦被 void，重建须给出完整 `upsert` 新版本。`kind` 一旦确立不可更改。
- 撤销版本只能指向 **严格更早期** 的有效 `receipt`，且一个 receipt 至多有一个有效 reversal。

### 2.2 事实存储语义

- `batches` 保存每个成功批次的 `seq`（从 1 连续递增）、`batch_id`、源字节 SHA-256、事件数和提交时间；`knowledge` 保存连续的 `knowledge_seq`；`raw_versions` 逐条保存原始版本（`seq,event_id,rev,op,kind,period,amount,target`），从不 UPDATE/DELETE；`contract` 亦不可变。三者都由 SQLite 触发器保护。
- 同一 `batch_id` 若源字节 SHA 与已提交记录相同：只返回原 `seq` 并标记 `duplicate:true`，不新增版本、不新增 seq、不改动任何表。同名但字节不同：整批拒绝。
- 一批次的“候选完整事实集合”= 该批全部变更应用到当前前态后的结果。只有它通过**全部合同期**的状态分摊与引用校验，才写入原始版本、批次与 `knowledge_seq`。校验与写入处在同一个 `BEGIN IMMEDIATE` 事务中，读到的就是同一前态；进程报错或 SQL 写失败时整体回滚，不会留下半批可见数据，`latest_seq` 不变。
- 追溯更正只要令任一后续收款超额、冲正失去目标（目标被撤销/不再是收款）或目标不再严格更早，整批拒绝，`knowledge_seq` 不增加；原版本与既有报告语义不变。

### 2.3 状态分摊语义

对给定知识截止的生效事实集合重算：

1. 初始本金 = 合同本金；未付利息 = 0。
2. 每期**先计息**：开期实际本金 × 每期有理利率，按非负 half-up 舍入到整数最小单位后加入未付利息；保留精确有理解（`exact_interest`）。未付利息不复利。
3. 同期内先处理全部有效 **reversal**，再处理全部 **receipt**；两类内部按 `event_id` 的 Unicode 码点序。
4. reversal 退回其目标 receipt 在**当前知识版本重算**得到的原本金/利息分摊：本金加回本金、利息加回未付利息。不把 reversal 当作普通负收款重新分摊。
5. receipt 先清未付利息，再减本金；金额超过当时“未付利息 + 本金”即拒绝。输出该项所清利息与本金。
6. 当期事件结束后的本金成为下一期开期本金。没有收款的本金不会按静态计划自动消失；末期可以仍欠款。

静态计划与事实集市是两条独立路径：静态计划永远按合同摊销，事实集市的期末本金取决于银行真实收款。

### 2.4 报告语义

`ledger-report` 在**同一个已提交数据库快照**（单个读事务）内：

1. 取 `seq <= known_seq` 的原始版本；
2. 为每个 `event_id` 选最高 `rev`；
3. 丢弃 void 版本；
4. 再按生效 `period <= through_period` 截断。

顺序不能颠倒：不能先按 period 筛版本再选 rev——否则更正把收款移到截断期之外时，旧版本会“复活”。报告只含 `known_seq` 及其之前的知识，绝不混入未来批次。

报告 JSON（`schema_version:2`，键排序、缩进固定，同前态复跑字节相同，金额全部为整数，无浮点）：

| 字段 | 含义 |
| --- | --- |
| `rounding` | 舍入规则说明 |
| `loan` | 不可变合同四字段 |
| `known_seq` / `through_period` | 两个截止 |
| `batches` | `known_seq` 以内全部成功批次，按 `seq` 升序：`{seq,batch_id,source_sha256,event_count}` |
| `periods` | 每期 `{period,opening_principal,exact_interest,interest,cumulative_interest,cumulative_net_receipts,unpaid_interest,closing_principal,remaining_debt,receipts{count,amount,interest_applied,principal_applied},reversals{count,interest_refunded,principal_refunded}}` |
| `events` | 截断后的有效事件，按实际结算顺序（逐期：先 reversal 后 receipt，各自按 event_id 码点序）：`{event_id,rev,kind,period,amount,target,interest_applied,principal_applied}` |
| `totals` | `{initial_principal,interest_accrued,receipts_total,reversal_refunds_total,net_receipts,receipt_principal,reversal_principal,remaining_principal,unpaid_interest,remaining_debt,checks{principal_conservation,balance_identity,period_chain}}` |

每个截止都必须满足：

- 本金守恒：`remaining_principal = initial_principal - receipt_principal + reversal_principal`；
- 报告恒等式：`initial_principal + interest_accrued - net_receipts = remaining_principal + unpaid_interest`；
- 期间链：第 1 期开期本金等于合同本金，逐期开期 = 上期期末。

不满足即内部校验失败、不发布。

### 2.5 失败定位

所有失败退出 2，向 stderr 输出可定位原因，例如：

- `batch_id 'B3' already committed with different source bytes`
- `event_id 'receipt-1' rev 2: expected rev 2`（实际给出 3 时）
- `event_id 'reversal-1' in period 2: reversal target 'receipt-1' has period 3, which is not strictly earlier`
- `event_id 'again' rev 1 in period 2: receipt 40 exceeds outstanding interest 0 + principal 20`
- `known_seq must be within 0..2`
- `through_period must be within 1..3`
- `ledger database does not exist: ...`

### 2.6 样例（`fixtures/ledger/`）

本金 100、3 期、每期利率 1/10：

```sh
loan-schedule ledger-init   --db run/loan.db --source fixtures/ledger/contract.json
loan-schedule ledger-import --db run/loan.db --source fixtures/ledger/batch-01-receipt.json
loan-schedule ledger-import --db run/loan.db --source fixtures/ledger/batch-02-reversal.json
loan-schedule ledger-report --db run/loan.db --known-seq 2 --through-period 3 --output run/plan.json
# p1 收款 50：开期 100/60/100，计息 10/6/10，未付利息 0/16/26，回冲 10 利息 + 40 本金，欠款 126

loan-schedule ledger-import --db run/loan.db --source fixtures/ledger/batch-03-correction.json
loan-schedule ledger-report --db run/loan.db --known-seq 3 --through-period 3 --output run/plan-3.json
# p1 更正为 70：回冲改为 10 利息 + 60 本金，p2 计息 4，未付利息 24

loan-schedule ledger-report --db run/loan.db --known-seq 2 --through-period 3 --output run/plan-2.json
# 旧截止仍返回 rev 1 与 126
```

若把收款更正移到 p3 而不撤销 p2 的 reversal，批次因“目标不再严格更早”整体拒绝；同批撤销该 reversal 则可提交，旧 `known_seq` 查询仍返回原分摊。

## 3. 事实保留与派生分摊为何保证历史截止与追溯依赖

- **原始版本只追加**：每次成功的 upsert/void 都以 `(seq,event_id)` 为键保存一份不可变记录，带其源批次 SHA。任何历史截止都能仅凭 `seq <= K` 的原始版本重建出当时的知识集合；触发器使已提交事实不可改写或删除。
- **知识序列连续且单调**：`knowledge_seq` 每成功一批加一；`known_seq=0..最新` 都可精确重放。拒绝批次不占用 seq，也不留下任何行。重复字节的批次不重复分配 seq。
- **没有跨提交的快照分摊**：报告里的每期余额、每个事件的利息/本金分摊都由“该截止的生效事实集合”完整重算（`allocation.allocate`），绝不复制上一版分摊。因此更正某一收款会自然带动其后所有期间与相关 reversal 的分摊，而旧截止的分摊仍由旧版本重算得到，二者互不污染。
- **reversal 依赖重算而非快照**：reversal 退回的是其目标 receipt 在当前知识版本下的分摊；追溯更正目标金额，reversal 的退本金/退利息随之改变；同一批里“更正收款 + 新增 reversal”按联合候选集校验，数组顺序不影响合法性。若更正令 reversal 失去严格更早关系或目标不再是有效收款，候选集校验失败，整批不提交。
- **同一事务内验证与提交**：所有读、校验、写都在一个 `BEGIN IMMEDIATE` 中完成，杜绝“先验后开事务”的竞态；读报告在单个只读事务中完成，杜绝“边读边改”而混入未来知识。
- **确定性输出**：键排序、事件按实际结算顺序、批次按 seq、金额为整数、有理数仅以 `"分子/分母"` 或整数字符串出现在 `exact_interest`，因此同一前态复跑字节相同。

## 4. 边界

不涉及外部服务、真实日期、并发发布或断电恢复保证；单操作者批处理。不认证利率合法性，不提供金融建议；样例均为原创合成数据。
