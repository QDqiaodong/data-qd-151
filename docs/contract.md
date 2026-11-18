# Equal-principal loan data schedule v3 · 静态计划、还款事实集市与压力现金流数据集

本仓提供三个互不混淆的能力。**静态计划**沿用 v1 契约，只按合同生成“应还”计划；它不表示银行真实收到了钱。**还款事实集市**（SQLite 单贷款账本）记录真实收款、撤销与追溯更正，并在任意历史知识截止点重算实际债务。**压力现金流数据集**（独立 SQLite 文件）在假设情景下逐贷款生成资产现金流、逐层生成证券偿付事实与池级资金平衡，供分析人员复核。

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

## 3. 压力现金流数据集（tranche-build）

```sh
loan-schedule tranche-build --source SCENARIO.json --output DATASET.db
```

该命令**完全独立**：只读取场景 JSON、只新建输出 SQLite，不读写静态入口的 JSON 报告，也不读写 ledger 账本数据库。失败退出 2 并在 stderr 给出可定位原因；任何解析、计算或 SQL 约束失败都保留旧输出库不动。成功后 stdout 输出汇总（`schema_version:1`、`command:"tranche-build"`、`source_sha256`、`horizon`、`loan_count`、`initial_principal`、`cleanup_period`、两层证券期末本金与息欠）。

### 3.1 严格输入契约

顶层恰为 `{pool_id, horizon, loans, exits, senior, junior, reserve, cleanup}`，未知字段拒绝；输入仍限 8MB、拒绝重复 JSON 键与非有限数；所有整数位置拒绝布尔。

- `pool_id`、每个 `loan_id` 沿用 `ident` 规则（非空、无首尾空白、≤128 字符、无控制字符）。
- `horizon`：1..48 整数。所有贷款均从第 1 期开始。
- `loans`：1..2000 个 `{loan_id, contract}`，`loan_id` 唯一；`contract` 沿用原四字段（principal 1..10^12、periods 1..10000 且 ≤ principal、rate_numerator 0..10^6、rate_denominator 1..10^9），本产品额外要求 `periods <= horizon`，且**每期利率 ≤ 1**（numerator ≤ denominator）。合同期数总和 ≤ 20000；贷款本金合计 ≤ 10^12。
- `senior`、`junior`：各为 `{principal, rate_numerator, rate_denominator}`。本金可为 0，两者本金合计必须**恰等于**贷款总本金；利率字段范围同合同且每期利率 ≤ 1。
- `reserve`：0..贷款总本金的整数，是发起人另行提供的初始准备金兼目标余额，**不属于证券本金**。
- `exits`：数组，每笔贷款至多一项，可缺省。
  - 提前结清：`{loan_id, kind:"prepay", period}`，period 在该贷款合同期内（1..periods）。
  - 违约：`{loan_id, kind:"default", period, recovery_numerator, recovery_denominator, recovery_lag}`；period 在合同期内；回收率为精确分数，numerator 0..10^9、denominator 1..10^9 且 numerator ≤ denominator；lag 1..48 且 `period + lag <= horizon`。
  - 引用未知贷款、同一贷款重复退出、退出对象含未知字段或未知 kind，一律拒绝。
- `cleanup`：`{threshold_numerator, threshold_denominator, price_numerator, price_denominator}`。阈值分数 ∈ [0,1]（numerator 0..2·10^9、denominator 1..10^9）；售价比例分数 ∈ [0,2]。
- 在“本金合计 ≤ 10^12、各期利率 ≤ 1、售价 ≤ 2 倍基数、horizon ≤ 48”的联合约束下，所有金额都可精确存入 SQLite 有符号 64 位整数；代码全程使用整数与 `Fraction`，无浮点。

### 3.2 资产端语义（逐贷款逐期）

合同现金流由**原四字段合同 + 原等额本金尾差规则 + 原 nonnegative half-up 规则**经同一个 `calculation.schedule` 生成，不另定静态本金算法；情景再决定哪些合同流实际发生。

1. 每期先处理**开期违约**：命中 default 的贷款，其正常剩余本金全部退出，本期不收合同本息；按“开期剩余本金 × 回收率”做**唯一一次 half-up** 得到待收回收款（`recovery_recognized`），记账为 `defaulted_principal = asset_loss + recovery_recognized`，待收款在 `period + recovery_lag` 到账。
2. 未违约贷款按原合同计划收当期利息与计划本金。
3. 命中 prepay 的贷款：收当期合同利息与**全部开期剩余本金**（`prepay_principal`），以此替代当期计划本金（当期 `contract_principal=0`）。
4. 随后收取本期到期的回收款（`recovery_received`），待收余额相应归零。
5. 已退出贷款后续合同现金流恒为 0；待收款在到账或被出售取消后归零。零流量期仍输出整行，不省略。

### 3.3 报价与清仓权的隔离

- 报价基数是**正常当期资产现金流发生之后**的“剩余正常本金 + 尚未到账回收款”；当期已经到账的回收已变现金，**不得再出售**，所以同期回收到账不会被双计。
- 剩余正常本金 + 待收回收为 0 时不报价（`quote_ineligible_reason='no_saleable_assets'`）。
- 否则仅当精确有理数判定 `剩余正常本金 ≤ 初始池本金 × 阈值` 成立时才可报价（不成立记 `above_threshold`，无浮点比较）。
- 报价 = 对每笔贷款的（剩余正常本金 + 未到账回收）乘售价比例，**逐贷款 half-up 后求和**；未来合同利息从不计入可出售本金。
- 证券利息以各层**开期证券本金**逐期 half-up 计入息欠；历史息欠不复利。
- 只有“当期现金 + 当前准备金 + 清仓价”足以**一次**清偿两层本金与全部息欠（`coverage_gap ≤ 0`）才行权；否则本期不出售任何资产、不取消任何回收、不耗用准备金做试算，资产保留并走正常瀑布（`cleanup_exercised=0`、`sale_proceeds=0`、`canceled_recovery=0`）。
- 行权当期：出售全部剩余正常本金与未到账回收，取消未来合同与回收现金流（`sold_principal`、`canceled_recovery`、`sale_proceeds` 只在此期出现且售价只入账一次），准备金、当期流入与售价合并后先清两层全部息欠、再清两层全部本金，余款归发起人，准备金归零。其后直到 horizon 只输出零流入、零证券债务的行：不再计息、不再有回收、不可二次报价或行权。

### 3.4 正常瀑布与准备金

未行权的每期按序：① 当期现金不足时动用准备金，先清 senior 全部息欠，再清 junior 全部息欠；② 剩余现金先把准备金补回 `reserve` 目标；③ 再还 senior 本金；④ 再还 junior 本金；⑤ 余现金归发起人（`sponsor_cash`，恒非负）。最后一期先把全部准备金释放进现金且该期不再补准备金（`reserve_released`）。本金与息欠任何时刻不得为负；资产损失**不自动减记**证券本金。资金不足的合法情景不被拒绝，期末未付证券本金与息欠如实披露。

### 3.5 每期守恒（发布前在引擎与 SQLite 两侧都校验）

- 资金恒等式：`期初准备金 + 当期合同流入 + 到账回收 + 实收售价 = 期末准备金 + 证券利息支付 + 证券本金支付 + 发起人现金`。
- 贷款本金守恒：每笔贷款 `初始本金 = 合同本金 + 提前结清本金 + 违约本金 + 出售本金`。
- 回收守恒：每笔贷款 `确认回收 = 到账回收 + 被取消回收`；待收款逐期滚动 `期末 = 期初 + 新确认 − 到账 − 取消`。
- 证券滚动：每层每期 `期末本金 = 期初本金 − 本金支付`，`期末息欠 = 期初息欠 + 本期应计 − 利息支付`。
- 清仓后所有资产本金、待收回收与两层证券本金/息欠全部归零，后续期全零。

### 3.6 SQLite 表结构（完整字段）

所有金额列为整数最小货币单位（非负，除 `coverage_gap` 可正可负可零）；全部表使用 `STRICT`，主键保证粒度唯一，外键保证来源可追踪。数据集头 `dataset_meta.format = 'data-qd-151-stress/1'`。

**源参数表**

- `dataset_meta(id, format, schema_version, source_sha256, pool_id)`：单例行（id=1），`source_sha256` 为输入字节 SHA-256（64 位十六进制）。
- `pool_config(id, pool_id, horizon, initial_principal, reserve_target)`：池级输入参数；id 外键引用 `dataset_meta(id)`。
- `loan_contracts(loan_id PK, ordinal, principal, periods, rate_numerator, rate_denominator)`：逐笔源合同；`ordinal` 保留输入数组位置，事实排序按 `loan_id` 以保证乱序输入下结果稳定。
- `exit_terms(loan_id PK/FK→loan_contracts, kind, exit_period, recovery_numerator, recovery_denominator, recovery_lag)`：逐笔退出参数；prepay 的三个回收字段为 NULL，default 全部非空。
- `tranche_terms(tranche PK, rank_order UNIQUE, principal, rate_numerator, rate_denominator)`：`tranche ∈ {senior, junior}`，rank_order 0/1 固定瀑布顺序。
- `cleanup_terms(id PK=1, threshold_numerator, threshold_denominator, price_numerator, price_denominator)`。

**逐贷款逐期资产事实 `asset_facts`**，主键 `(loan_id, period)`，外键 `loan_id→loan_contracts`，每贷款 × 每 horizon 期必有一行：

| 字段 | 含义 |
| --- | --- |
| `opening_principal` / `closing_principal` | 期初/期末正常剩余本金 |
| `contract_interest` | 本期实际发生的合同计划利息（违约期为 0） |
| `contract_principal` | 本期实际发生的合同计划本金（prepay/违约期为 0） |
| `prepay_principal` | 提前结清收取的全部开期剩余本金 |
| `defaulted_principal` | 开期违约退出的本金 |
| `asset_loss` | 违约本金 − 确认回收的损失部分 |
| `recovery_recognized` | 违约期 half-up 确认的唯一待收回收款 |
| `recovery_received` | 本期实际到账的回收 |
| `pending_open` / `pending_close` | 期初/期末尚未到账的待收回收 |
| `sold_principal` | 清仓当期被出售的正常剩余本金 |
| `canceled_recovery` | 清仓当期被取消的未到账回收 |
| `sale_proceeds` | 清仓当期逐贷款 half-up 后入账的售价 |
| `asset_cash_in` | 该贷款本期总现金流入（合同本息 + 提前结清 + 到账回收 + 售价） |
| `status` | `active` / `defaulted` / `prepaid` / `closed` / `sold` |

**逐层逐期证券事实 `tranche_facts`**，主键 `(tranche, period)`，外键 `tranche→tranche_terms`，两层 × 每 horizon 期必有一行：`opening_principal, opening_unpaid_interest, accrued_interest, interest_paid, principal_paid, closing_principal, closing_unpaid_interest`。

**池级逐期资金平衡 `pool_facts`**，主键 `period`，每 horizon 期必有一行：

| 字段 | 含义 |
| --- | --- |
| `opening_reserve` / `closing_reserve` | 期初/期末准备金 |
| `reserve_used` / `reserve_replenished` / `reserve_released` | 准备金动用 / 补回 / 末期释放 |
| `contract_cash_in` / `recovery_cash_in` / `sale_proceeds` | 三类资产现金（与 asset_facts 聚合一致） |
| `total_asset_principal_open/close` | 池级正常本金期初/期末 |
| `total_pending_open/close` | 池级待收回收期初/期末 |
| `quote_offered` | 本期是否形成报价（0/1） |
| `quote_ineligible_reason` | 未报价原因：`no_saleable_assets` 或 `above_threshold`；报价行为 NULL |
| `quote_price` | 逐贷款 half-up 求和的清仓报价（未报价为 0） |
| `cleanup_exercised` | 本期是否行权（0/1，全程至多一个 1） |
| `total_debt_at_quote` | 报价时点两层本金 + 全部息欠 |
| `coverage_gap` | 报价行的 `总债务 − 当期现金 − 准备金 − 报价`；≤0 才可行权；非报价行为 0 |
| `interest_paid` / `principal_paid` | 两层证券利息/本金支付合计（与 tranche_facts 聚合一致） |
| `sponsor_cash` | 余归发起人的现金 |

表级 CHECK 还强制：未报价则报价与行权必为 0；报价但未行权则 `coverage_gap > 0`。

**复核视图**：`v_loan_principal_conservation`、`v_recovery_conservation`、`v_tranche_rollforward`、`v_pool_cash_conservation` 分别暴露上述四类守恒的逐行 `balanced` 标志，分析人员可直接 `SELECT ... WHERE balanced=0` 复核；发布流程本身也会查询这些视图，任一不平衡即拒绝发布。

### 3.7 原子发布、隔离与复跑

- 构建全程在目标目录下的临时库（`.tranche-build-*.db`）内完成：建表、插入、`PRAGMA foreign_key_check`、`PRAGMA integrity_check`、行数与全部守恒视图检查通过、事务提交、连接关闭后，才以 `os.replace` 原子替换目标库。不会先覆盖旧库再计算；失败时临时库删除、旧库字节不变。
- 输出不得与输入同路径、不得是符号链接；输入文件只读不修改。
- 事实按 `loan_id`/`tranche`/`period` 确定性排序，输入数组乱序不影响有序 SQL 查询结果；复跑不要求 SQLite 文件字节一致，但有序查询的值一致。

### 3.8 样例（`fixtures/stress/`）

两个合成情景共享同一资产池：零息 100/2 期贷款 A 在第 1 期开期违约、回收率 1、滞后 2 期；零息 100/2 期贷款 B 正常摊销（每期本金 50）；senior 120、junior 80、零准备金、清仓阈值 1/4、售价比例 1。

- `scenario-cleanup.json`（两层零息）：第 1 期正常现金 50、剩余正常本金 50、待回收 100，报价 150，资金 50+150=200 一次清偿，行权；A 的待回收 100 被 `canceled_recovery` 取消、第 3 期回收为 0。
- `scenario-hold.json`（仅 junior 期利率 1/10）：第 1 期 junior 应计息 8，总债务 208，`coverage_gap=8`，报价 150 也不足，不行权、不出售、不取消；正常瀑布付息 8、还 senior 本金 42；第 3 期 100 回收照常到账。

### 3.9 报价/行权隔离与未来现金取消为何正确

- **报价与行权是两个判断**：报价只回答“现在能卖多少钱”（基于正常流之后的剩余本金与未到账回收，逐贷款 half-up 求和），行权还要回答“钱够不够一次还清两层全部本息”。不足时资产、回收、准备金均保持原样，数据集里 `quote_price` 保留报价但 `sale_proceeds/canceled_recovery` 全为 0，复核者能直接看到“报过价但未行权”的原因（`coverage_gap`）。
- **到账回收不双计**：回收在资产流阶段先到账成为现金，报价基数只取期末仍未到账的 `pending_close` 与正常本金，已到账金额不会再出现在任何贷款的售价基数里。
- **未来现金取消有显式表示**：行权把每笔剩余贷款的正常本金写入 `sold_principal`、未到账回收写入 `canceled_recovery`、售价写入 `sale_proceeds`，三者在同一期闭合；之后各期整行保留但所有流量为 0，`status='sold'`，池级也不再报价。守恒视图保证“确认回收 = 到账 + 取消”，被取消的钱不会凭空消失，也不会在后期再次到账。

## 3A. 采购标段授予数据集（contract-award）

```sh
contract-award --source PROCUREMENT.json --output AWARDS.json
loan-schedule contract-award --source PROCUREMENT.json --output AWARDS.json
```

独立命令，只读取输入 JSON、只发布输出 JSON，不访问网络、数据库、静态计划或 ledger 账本。现有命令与合同行为不变。失败退出 2 并在 stderr 给出可定位原因，且**不覆盖**已有输出；输入与输出同路径、输出为符号链接均拒绝。源字节 SHA-256 由既有受限读取器 `common.read` 取得，输出经既有安全 writer `common.save` 原子替换（临时文件 + `os.replace`）。

### 3A.1 严格输入契约

顶层恰为 `{procurement_id, lots, suppliers, bids}`。沿用仓库既有受限 JSON 读取器：≤8MB、UTF-8 严格 JSON、拒绝重复键、拒绝 `NaN`/`Infinity` 等非有限数。所有整数位置拒绝布尔冒充。未知或缺失字段（顶层与每个嵌套对象）都拒绝整份文件，不静默丢弃任何行。

- `procurement_id`、`lot_id`、`supplier_id`、`bid_id` 与证书 ID 全部沿用既有 `ident` 规则：非空字符串、无首尾空白、≤128 字符、无控制字符。
- `lots`：1..200 项，每项恰为 `{lot_id, required_certificates}`；`lot_id` 唯一；`required_certificates` 为字符串数组，不得含重复值。
- `suppliers`：1..100 项，每项恰为 `{supplier_id, capacity, certificates}`；`supplier_id` 唯一；`capacity` 为 0..标段数的整数（按供应商整体计）；`certificates` 不得含重复值。
- `bids`：0..5000 项，每项恰为 `{bid_id, lot_id, supplier_id, amount}`；`bid_id` 唯一；`amount` 为 1..10^12 整数；每个 `(supplier_id, lot_id)` 至多一份报价；`lot_id`/`supplier_id` 必须引用已声明的 lot/supplier，悬空引用拒绝。
- 任一结构或范围错误拒绝整份采购文件，退出 2。

### 3A.2 资格判定与授标目标

一条报价成为**可授标边**当且仅当：供应商 `certificates` 覆盖该标段 `required_certificates` 的每个成员，且该供应商与该标段的报价存在（每对至多一份）。不合格报价**仍保留在输出中**，逐行列出 `missing_certificates`，但绝不作为授标边；容量按供应商整体计算。

在所有可行授予方案（每标段至多一个供应商、每供应商中标标段数 ≤ `capacity`、只经合格边）中按三级目标择优：

1. **最大化授标标段数**（未授标标段不计为零价中标）；
2. 再**最小化中标总价**（`amount` 之和）；
3. 再取**字典序最小方案**：把中标行的 `(lot_id, supplier_id, bid_id)` 排序后形成的完整序列作为比较键，仅当覆盖数与总价都相同后才比较该序列。

选择不依赖输入数组行序：校验阶段即按 `lot_id`、`supplier_id`、`(lot_id,supplier_id,bid_id)` 排序归一化。实现用一次最小费用最大流把三级目标折叠为单一任意精度整数目标（总价乘以 `2^E` 加位置权重），因此在同价时由完整序列破同分，且截断误差无法压过一个最小金额单位。

### 3A.3 输出契约（`schema_version:1`）

键排序、缩进固定、`ensure_ascii=False`、无浮点，同一输入重跑字节一致。

- `schema_version`、`procurement_id`、`source_sha256`（源字节 SHA-256）。
- `objective`：`{rule, lot_count, supplier_count, bid_count, qualified_bid_count, awarded_lot_count, awarded_total_amount, unawarded_lot_count}`。`rule` 是三级目标的文字描述。
- `lots`：按 `lot_id` 升序，逐 lot 输出
  `{lot_id, required_certificates, awarded, awarded_bid_id, awarded_supplier_id, awarded_amount, unawarded_reason, candidates}`。`candidates` 按 `(supplier_id,bid_id)` 升序，逐条为 `{bid_id, supplier_id, amount, certificate_qualified, missing_certificates, selected}`，包含该 lot 的**全部**输入报价（含不合格者）。
- `suppliers`：按 `supplier_id` 升序，逐条为 `{supplier_id, capacity, awarded_lot_count, awarded_lot_ids, awarded_total_amount, remaining_capacity}`；`awarded_lot_ids` 按 `lot_id` 升序。

`unawarded_reason` 仅用于未授标 lot，按以下**优先级**判定（依据该 lot 实际报价与资格状态，不被其他原因覆盖）：

1. `no_bid`：该 lot 没有任何报价；
2. `no_qualified_bid`：有报价但没有证书合格的报价；
3. `capacity_competition`：存在证书合格报价但该 lot 未进入最终最优方案（被容量或其他标段的竞争挤出）。

已授标 lot 的 `unawarded_reason` 为 `null`；无合格报价的 lot 必须未授标，绝不补造零价报价。

### 3A.4 发布前校验

`award.validate_result` 在写盘前独立复核：lot/supplier 行与输入一一对应且有序、每条候选与原 bid 的 ID/供应商/金额一致、证书状态与缺失证书一致、每 lot 至多一个 `selected`、每个中标 bid 均来自输入且其供应商证书覆盖该 lot 需求、无 `(supplier_id)` 超容量、各 lot 只授一次、objective 汇总与逐行一致、未授标原因符合优先级。任一不符即 `ValueError`，退出 2 且保留旧输出。

### 3A.5 样例与测试

`fixtures/award/procurement.json` 为原创合成采购批次（7 个 lot、4 个供应商、11 份报价），刻意覆盖：资质不合格（缺 `SECRET`）、`capacity=0` 的供应商、容量争抢导致的全局取舍、仅一份合格报价、以及 `no_bid`/`no_qualified_bid`/`capacity_competition` 三种未授标原因。

`tests/test_award.py` 用**独立于生产分配代码的穷举 oracle**（递归枚举全部可行授标集并取三级最优）对 120 个随机小实例及固定样例逐一比对，另覆盖资格阻断、容量冲突、标段唯一、同价字典序破同分、乱序输入稳定、退出码 2 保留旧输出、输出符号链接拒绝、独立 console entry point。

## 3B. 电表分时电量与日费用数据集（meter-tou）

```sh
meter-tou --source METERS.json --output REPORT.json
loan-schedule meter-tou --source METERS.json --output REPORT.json
```

独立命令：只读取一个严格 JSON、只发布一个 JSON 报告。不访问网络、数据库、静态计划、ledger 或压力数据集，现有命令行为不变。时区规则**只**来自 Python 标准库 `zoneinfo` 读取的系统 IANA 数据；代码中不写死任何夏令时偏移。失败（解析、时区、计算、结果校验、发布）一律退出 2、在 stderr 说明原因并保留旧报告；输入与输出同路径、输出为符号链接均拒绝；源 ≤8MB，源字节 SHA-256 由既有 `common.read` 取得，输出经既有 `common.save` 原子替换。

### 3B.1 严格输入契约

顶层恰为 `{dataset_id, timezone, window_start, window_end, meters, tariffs, intervals}`；未知/缺失字段、重复键、`NaN`/`Infinity`、布尔冒充整数全部拒绝。

- `dataset_id`、`meter_id`、`tariff_id`、`interval_id` 沿用既有 `ident` 规则。
- `timezone` 只允许 `UTC`、`Asia/Shanghai`、`America/New_York`、`Europe/Berlin`（大小写敏感）。名字不在列表、`ZoneInfo` 未知名称或系统时区数据不可用都明确失败，**绝不回退 UTC**。
- `window_start`、`window_end` 为整数 Unix 秒，均在 `2000-01-01T00:00:00Z`（946684800）至 `2100-01-01T00:00:00Z`（4102444800）之间；`window_start < window_end` 且跨度 ≤ `31*86400` 秒。窗口左闭右开。
- `meters`：1..200 个唯一 `ident`，声明哪些电表需要在报告中逐日出现。
- `tariffs`：1..96 项，每项恰为 `{tariff_id, start_minute, end_minute, rate_numerator, rate_denominator}`。`tariff_id` 唯一；`start_minute`/`end_minute` 为 0..1440 整数且 `start_minute < end_minute`；`rate_numerator` 为 0..10^6、`rate_denominator` 为 1..10^6，二者构成非负精确分数，单位为**每 Wh 的最小货币单位**（允许 0 费率，无小数货币）。按 `start_minute` 排序后必须无缝、无重叠地覆盖 `[0,1440)`：首项从 0 起，逐项首尾相接，末项止于 1440。费率每天按**当地墙钟分钟**重复，不区分工作日/节假日。
- `intervals`：0..20000 项，每项恰为 `{interval_id, meter_id, start, end, energy_wh}`。`interval_id` 全局唯一；`meter_id` 必须引用已声明电表；`start`/`end` 为整数 Unix 秒、均在上述时间范围内、`start < end`、时长 ≤ `31*86400`；`energy_wh` 为 0..10^12 整数。**同一电表**的原始 UTC 区间不得相交（`start < 前一 end` 即拒绝）；端点相接允许，不同电表可同时段。重复行拒绝，不去重、不择优覆盖。所有区间先全量校验再裁剪：窗口外的区间同样参与 ID、引用、范围与相交检查，不能用窗口裁剪掩盖错误。

### 3B.2 分摊与片段语义

1. 每条区间的电量按**原始 UTC 时长**均匀分摊（`energy_wh / (end-start)` Wh 每秒），与窗口裁剪无关；**不得**把裁剪后剩余电量重新按新时长分配。
2. 先按报告窗口裁剪（`[max(start,window_start), min(end,window_end))`），得到 `clipped_seconds`；无正秒数交集的区间不产生片段。
3. 再在该裁剪区间内按三类边界切成最大连续片段：当地日界（当地 00:00）、费率边界（`start_minute` 对应的当地墙钟时刻）、UTC 偏移变化（夏令时切换点，按 UTC 秒二分定位）。片段内的时刻保留秒级精度，输入的秒数端点不取整到分钟。
4. 夏令时回拨时，同一当地墙钟时段会经过两次，两次必须**分别**按各自 UTC 偏移计量（例如 America/New_York 2025-11-02 的 01:00 分别带 -14400 与 -18000）；夏令时前拨跳过的不存在当地时段**不补量**，相关费率得到精确 0。
5. 「最大连续片段」仅在同一**原区间**内、相邻且 `(local_date, utc_offset_seconds, tariff_id)` 完全一致时才合并；不同原区间之间绝不合并（即使端点相接）。结果不依赖输入顺序。
6. 片段起点处的当地时刻决定所属当地日与费率：当地 00:00 归新日，费率终点归下一档。

片段字段：`{interval_id, meter_id, start, end, local_date, utc_offset_seconds, tariff_id, seconds, energy_wh_exact, cost_exact}`。`seconds == end-start`；`energy_wh_exact` 为 `{numerator,denominator}` 约分精确分数（分母正数）；`cost_exact = energy × 费率` 亦为精确分数。

### 3B.3 当地日、覆盖与费用

- 报告为**每个已声明电表 × 窗口内每个当地日**输出一行（两者笛卡尔积），包括无任何观测的日；当地日由系统时区规则从窗口端点推导，窗口可能只覆盖某当地日的一部分。
- `expected_seconds` = 该当地日与报告窗口的**物理**秒数交集（自动反映 82800/86400/90000 等）；不允许固定按 86400 判断。
- `covered_seconds` = 该电表在该当地日、窗口内、被有效输入区间覆盖的物理秒数（相交区间按半开集合并计算，不重复计）。
- `missing_seconds = expected_seconds - covered_seconds`；`coverage_status`：`covered == expected` → `complete`，`0 < covered < expected` → `partial`，`covered == 0` → `absent`。缺失观测只作为缺口报告，**绝不**补成零电量事实。
- 日行字段：`{meter_id, local_date, expected_seconds, covered_seconds, missing_seconds, coverage_status, energy_wh_exact, cost_exact, cost_integer, tariffs}`。`tariffs` 按 `tariff_id` 升序，**列出全部已配置费率**（未经者填精确 0），逐项 `{tariff_id, energy_wh_exact, cost_exact}`。
- 日整数费用 `cost_integer` 由当日精确费用 `cost_exact` 做**一次**非负 half-up 得到；日 `cost_exact` 是该日所有片段费用之和，而非逐片段舍入后相加。总费用 `totals.cost_integer` 为各电表各日整数费用之和；`totals.cost_exact` 为全窗口精确费用，二者不必相等。
- 电表总结 `meters` 按 `meter_id` 升序，逐条 `{meter_id, interval_count, day_count, segment_count, expected_seconds, covered_seconds, missing_seconds, energy_wh_exact, cost_exact, cost_integer}`；其中 `cost_integer` 为该电表各日整数费用之和（不是对其精确费用再舍入一次），`interval_count` 为原始输入区间条数（含窗口外）。`totals` 另含 `meter_count`、`day_count`、`segment_count` 与窗口覆盖/电量/费用合计。

### 3B.4 输出契约（`schema_version:1`）

键排序、缩进固定、`ensure_ascii=False`、无浮点。顶层：
`{schema_version, dataset_id, source_sha256, timezone, window:{start,end,seconds}, tariffs, totals, meters, days, segments}`。
`tariffs` 按 `tariff_id` 升序原样输出配置（分钟与费率分子/分母）；`segments` 按 `(meter_id,start,end,interval_id)` 升序；`days` 按 `(meter_id,local_date)` 升序。同一输入字节（即使数组乱序）业务结果相同，同一源字节报告**字节一致**。

### 3B.5 发布前校验

`meter_tou.validate_result` 在 `common.save` 前独立复核：schema/dataset/timezone/window/sha 一致；每个片段位于窗口与其原区间内、`seconds` 正确、当地日/偏移/费率与 `zoneinfo` 及墙钟起点一致、片段未跨越日界/费率界/偏移变化、电量严格等于“原电量 × 片段秒/原秒”、费用等于电量乘费率；同一原区间的片段秒数与电量之和恰等其裁剪值；无应合并而未合并的相邻片段；每日及每费率汇总、电表总计与总计均等于片段精确和；`covered_seconds ≤ expected_seconds` 且 `missing_seconds` 一致；日整数费用确为 half-up。任一不符即 `ValueError`，退出 2 且保留旧输出。

### 3B.6 样例与测试

`fixtures/meter/` 为原创合成数据：`sample.json`（纽约回拨日、两表、窗口内部分覆盖与整日覆盖）、`sparse.json`（上海、完全未观测电表）、`fall-back.json` 与 `spring-forward.json`（两段验收用例）、`berlin-midnight.json`（非整分钟端点跨当地午夜与费率边界）。

`tests/test_meter_tou.py` 覆盖两条验收用例（90000/82800 秒、200/2300 Wh、2900/2300 费用、重复 01 点双偏移）、UTC 窗口裁剪（`covered=3600, missing=3600`）、两个半费区间只舍入一次（日整数 1）、无区间电表逐日 `absent`、非整分钟端点、相邻片段合并且跨区间不合并、跨午夜/跨窗口、DST 前拨/回拨、重叠与重复行拒绝（含窗口外）、乱序稳定与字节一致、退出 2 保留旧报告、同路径与符号链接拒绝、8MB 限制；另有逐秒枚举 oracle 交叉核对电量与覆盖秒数。

## 3C. 关系表候选键画像数据集（table-keys）

```sh
table-keys --source RELATION.json --output REPORT.json
loan-schedule table-keys --source RELATION.json --output REPORT.json
python -m loan_schedule.cli table-keys --source RELATION.json --output REPORT.json
```

独立命令：只读取一个严格 JSON 关系、只发布一个 JSON 报告，不访问网络、数据库或其他数据集，既有命令行为完全不变。失败（解析、搜索、结果核验、发布）一律退出 2 并在 stderr 给出原因，保留旧报告；输入与输出同路径、输出为符号链接均拒绝。源 ≤8MB，源字节 SHA-256 由既有 `common.read` 取得，输出经既有 `common.save` 原子替换（临时文件 + `os.replace`）。

### 3C.1 严格输入契约

顶层恰为 `{dataset_id, columns, rows}`；每个 row 对象恰为 `{row_id, values}`。沿用受限读取器：≤8MB、UTF-8 严格 JSON、拒绝重复 JSON 键、拒绝 `NaN`/`Infinity` 等非有限数。未知/缺失字段（顶层与每个 row）、重复列名、重复 `row_id`、`values` 长度与列数不符都拒绝整份输入，绝不静默丢弃行。

- `dataset_id`、列名、`row_id` 沿用既有 `ident` 规则（非空、无首尾空白、≤128 字符、无控制字符）。
- `columns`：1..24 个唯一 ident。
- `rows`：0..20000 项，`row_id` 唯一；`values` 与 `columns` 等长且按列序对齐。
- 单元格只允许：`null`、布尔、范围 `[-10^12, 10^12]` 的整数、长度 ≤256 的字符串。拒绝浮点数、列表、对象等嵌套值；布尔不得冒充整数。
- 字符串保留原始 Unicode 码点：不做大小写转换、裁边或任何归一化；`"null"` 与 `null` 不相等。整数 `0` 与 `-0` 相等。

### 3C.2 类型相等语义

比较按**类型与值同时相等**实现：每个接受的单元格在内部带类型标记，因此 `true`、`1`、`"1"` 两两不等；两个 `null` 相等；同类型再比值。列名与 `row_id` 只是元数据，**`row_id` 不参与唯一性判断**；值完全相同但 `row_id` 不同的行必须保留并参与全部判定。

### 3C.3 列子集上的两个包含边界

对全部列子集（空集也参与）精确求两类边界，这里的最小/最大是**集合包含关系**，与列数多少无关：同一份报告可同时含一列键和两列键。

- **唯一（键）**：不存在两个不同 `row_id` 在选定列上逐值相等。**最小唯一组**：本身唯一，但删除任意一列后不再唯一。
- **非唯一（非键）**：至少有一行对在选定列上逐值相等。**最大非唯一组**：本身非唯一，但添加任意未选列后变唯一。
- 0 或 1 行时唯一边界恰为 `[[]]`（空列组是键），非唯一边界为空。
- 存在全列值仍重复的行时唯一边界为空，非唯一边界仅含全列组。
- 全部合法列子集必须恰好被两侧解释：包含某个最小键即为唯一，否则必须包含于某个最大非键，二者互补且无遗漏。不允许只返回最短键、单个键、抽样结论或截断前缀。

### 3C.4 搜索为何不漏报

1. **等价列折叠**：值向量完全相同的列可互换，先按向量归为等价类；类上的答案可无损还原为原列答案（类键按“每类取一列”展开为全部互换键，最大非键按“整类包含/排除”展开）。
2. **两条互证的穷举路径**（均不抽样）：
   - **行对协议掩码路径**（内存预算内）：对每个行对计算其在哪些类上相等的精确位掩码；列组非唯一当且仅当它包含于某个行对的协议掩码，故全部协议掩码中的包含极大者恰为最大非键。
   - **增量划分 DFS 路径**（行对表超预算时）：维护当前列集对全部行诱导的精确划分，逐列分裂块；某列集无法再用任何剩余列拆分仍含 ≥2 行的块时即为极大协议集。每个不同列集至多处理一次，每个极大协议集必是叶节点，因此不会漏报。
3. **对偶求键**：最大非键补集族的全部**包含极小击中集**恰为包含极小唯一组（最小键），由独立的传递闭包枚举求得并做极小性核验。
4. 归一化阶段即把列按 Unicode 码点、行按 `row_id` 排序，并重排 `values`，所以行序、列序置换不改变业务结果。

### 3C.5 碰撞见证

每个最大非唯一列组附 `witness=[a,b]`：先令 `a<b`（按 `row_id` 的 Unicode 序），再在该列组上全部碰撞行对中取字典序最小的一对。见证在同一规范化输入上回查，并在发布前独立重扫确认它确实逐值相等且确为最小对。

### 3C.6 输出契约（`schema_version:1`）

键排序、`ensure_ascii=False`、无浮点，同字节输入复跑字节一致。顶层字段：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 固定 1 |
| `dataset_id` | 回显输入标识 |
| `source_sha256` | 源字节 SHA-256（64 位十六进制） |
| `row_count` | 参与判定的行数（含完全重复值行） |
| `columns` | 规范列序：按 Unicode 码点排序后的列名数组 |
| `minimal_keys` | 列名数组的数组；组内列名按码点排序，边界先按列数再按列名序列排序 |
| `maximal_nonkeys` | 每项恰含 `columns`（列名数组）与 `witness`（`[a,b]`）；同样排序 |

### 3C.7 发布前核验

`table_keys.validate_result` 在 `common.save` 前独立复核，任一不符即 `ValueError`、退出 2 且旧报告字节不变：

- 用**另一条**穷举路径（行对表 ↔ 划分 DFS）重算两个完整边界并逐项比对，使单条路径的实现缺陷无法自洽通过；
- 每个最小键不包含于任何最大非键（唯一）且删任一类即落入某非键（极小）；每个最大非键加任一缺席类都不再被任何非键包含（极大）；
- 见证行存在、`a<b`、在该组逐值相等且为字典序最小碰撞对；
- 对小规模类格（≤2^20 子集）穷举每个合法子集，确认“含某最小键 ⇔ 唯一，否则含于某最大非键”的互补覆盖；
- 键数量受限时再对原始带类型行直接做唯一性检查；列组排序、字段集合、sha 与行数一并核验。

### 3C.8 资源验收与运行方法

验收规模例：4096 行覆盖 12 位全部二进制向量，每位复制为两列共 24 列（`b00a/b00b`..`b11a/b11b`，`b00` 为最低位、`b11` 为最高位，两份复制列取相同 0/1），`row_id` 为 `r0000`..`r4095`。最小键恰为从每对复制列中各取一列（共 2^12=4096 个），最大非键恰为全列删除某一对复制列（12 个），见证分别为 `(r0000, r{2^p})`。在 Linux 单 CPU、512MiB 内存、60 秒墙钟内完成完整 CLI 报告发布（不含安装时间）；实测搜索与双路径核验、JSON 发布合计约 20 秒、峰值常驻约 62MiB。

```sh
# 生成规模例并计时/计量（标准库 resource 取子进程峰值 RSS）
python tests/tools/make_scale_fixture.py --rows 4096 --output /tmp/scale.json
/usr/bin/env python -m loan_schedule.cli table-keys --source /tmp/scale.json --output /tmp/scale-report.json
```

`tests/test_table_keys.py` 用**不调用生产边界搜索或生产唯一性判定**的独立全格枚举 oracle，对至多 8 列的随机表逐一核对两个完整边界与最小碰撞对，并强制划分 DFS 路径再核一遍；另覆盖 0/1 行、全列重复、常量列、等价列、单列全空、`true/1/"1"/null` 类型区分、`0`/`-0` 与 `"null"`/`null`、行列置换稳定、字节一致、退出 2 保留旧报告、同路径与符号链接拒绝及全部非法输入拒绝。

## 4. 事实保留与派生分摊为何保证历史截止与追溯依赖

- **原始版本只追加**：每次成功的 upsert/void 都以 `(seq,event_id)` 为键保存一份不可变记录，带其源批次 SHA。任何历史截止都能仅凭 `seq <= K` 的原始版本重建出当时的知识集合；触发器使已提交事实不可改写或删除。
- **知识序列连续且单调**：`knowledge_seq` 每成功一批加一；`known_seq=0..最新` 都可精确重放。拒绝批次不占用 seq，也不留下任何行。重复字节的批次不重复分配 seq。
- **没有跨提交的快照分摊**：报告里的每期余额、每个事件的利息/本金分摊都由“该截止的生效事实集合”完整重算（`allocation.allocate`），绝不复制上一版分摊。因此更正某一收款会自然带动其后所有期间与相关 reversal 的分摊，而旧截止的分摊仍由旧版本重算得到，二者互不污染。
- **reversal 依赖重算而非快照**：reversal 退回的是其目标 receipt 在当前知识版本下的分摊；追溯更正目标金额，reversal 的退本金/退利息随之改变；同一批里“更正收款 + 新增 reversal”按联合候选集校验，数组顺序不影响合法性。若更正令 reversal 失去严格更早关系或目标不再是有效收款，候选集校验失败，整批不提交。
- **同一事务内验证与提交**：所有读、校验、写都在一个 `BEGIN IMMEDIATE` 中完成，杜绝“先验后开事务”的竞态；读报告在单个只读事务中完成，杜绝“边读边改”而混入未来知识。
- **确定性输出**：键排序、事件按实际结算顺序、批次按 seq、金额为整数、有理数仅以 `"分子/分母"` 或整数字符串出现在 `exact_interest`，因此同一前态复跑字节相同。

## 5. 边界

不涉及外部服务、真实日期、并发发布或断电恢复保证；单操作者批处理。不认证利率合法性，不提供金融建议；样例均为原创合成数据。压力数据集只在本地用 Python 标准库与 SQLite 构建，不访问网络。
