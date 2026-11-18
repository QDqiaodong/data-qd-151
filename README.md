# data-qd-151 · 等额本金贷款计划、还款事实集市与压力现金流数据集

静态等额本金计划 + 可审计的单贷款还款事实集市 + 独立 SQLite 压力情景现金流数据集。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

## 安装与演示

Python >=3.11，运行和测试仅用标准库；先确认 `python3 --version`。
每仓建立独立环境，构建工具锁版本。首次安装无公共缓存时可能联网；业务运行不访问网络。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
.venv/bin/loan-schedule --source fixtures/input.json --output run/report.json
.venv/bin/python -m unittest discover -s tests -v
```

本地已有wheel时，安装构建工具可加 `--no-index --find-links <公共wheel缓存目录>`。
修改源码后重新安装。本仓不共享其他仓环境，不依赖Docker、外部账号或持续服务。

输入JSON最多8MB，拒绝重复键、非有限数值和未定义字段；源字节SHA256随结果保留。
输出是可独立读取的JSON；成功后原子替换，输入/计算失败退出2并保留旧报告。
输入和输出不得相同，输出符号链接拒绝。单操作者批处理，不承诺并发发布或断电恢复。
同输入重跑不累加。测试使用临时目录，运行产物在忽略的run/。

src/loan_schedule/model.py 为业务模型，allocation.py 为事实分摊引擎，ledger.py 负责不可变事实与知识截止，
sqlstore.py 管理 SQLite 事务与表结构，cdc.py 负责交错 CDC 事务日志的提交可见快照与逐事务审计，
common.py 提供受限JSON及报告发布，cli.py 衔接数据链路。
fixtures 为明确自制样例，代码与样例采用MIT许可。

## CDC 事务日志核对（cdc-materialize）

```sh
.venv/bin/loan-schedule cdc-materialize --source fixtures/cdc/linked-acceptance.json --output run/cdc.json
# 等价入口：python -m loan_schedule.cli cdc-materialize ...；专用 console 脚本 cdc-materialize ...
```

把多表交错、带 begin/commit/abort 边界的行变更日志物化为最终提交可见关系，并逐事务输出
accepted/rejected/aborted/open 审计与首个错误证据（`missing_row`/`before_mismatch`/
`primary_key_conflict`/`foreign_key`）。commit 时在最新已接纳快照的私有副本上按 change 原序重演，
before 像按提交时刻重新核对；外键在整笔重演成功后对全关系（含未改动子表）联合裁决，支持循环、
自引用与 null 分量豁免。结构/框架错误退出 2 并保留旧报告；业务拒绝进入报告且退出 0。
完整契约见 [docs/cdc-materialize.md](docs/cdc-materialize.md)，原创样例在 `fixtures/cdc/`。

## 静态计划（v1 契约未变）

```sh
.venv/bin/loan-schedule --source fixtures/input.json --output run/report.json
```

principal为1..10^12整数货币最小单位，periods=1..10000且不得超过本金最小单位数；rate_numerator=0..10^6、rate_denominator=1..10^9表示每期利率，非年利率。
前periods-1期偿还floor(principal/periods)，末期偿还全部尾差；每期开期余额乘每期利率，以非负half-up逐期舍入至整数最小单位，保留舍入前有理数。无复利、日期、提前还款、费用或逾期；不是等额本息。总本金必须恰等初始本金，末余额0。
样例本金1000/3期、利率1%，本金333/333/334，利息10/7/3，总利息20、总支付1020。
静态计划只表示合同“应还”，不表示银行真实收到了钱。

## 还款事实集市

记录银行真实收款、撤销与追溯更正，并可在任意历史知识截止点重算实际债务：

```sh
.venv/bin/loan-schedule ledger-init   --db run/loan.db --source fixtures/ledger/contract.json
.venv/bin/loan-schedule ledger-import --db run/loan.db --source fixtures/ledger/batch-01-receipt.json
.venv/bin/loan-schedule ledger-import --db run/loan.db --source fixtures/ledger/batch-02-reversal.json
.venv/bin/loan-schedule ledger-report --db run/loan.db --known-seq 2 --through-period 3 --output run/plan-2.json
.venv/bin/loan-schedule ledger-import --db run/loan.db --source fixtures/ledger/batch-03-correction.json
.venv/bin/loan-schedule ledger-report --db run/loan.db --known-seq 3 --through-period 3 --output run/plan-3.json
```

- `ledger-init` 以四字段合同建立不可变账本，拒绝覆盖已有数据库（含符号链接/零字节文件），seq 从 0 开始，初始化失败不留下半成品。
- `ledger-import` 读 ≤8MB、≤20000 事件的批次；支持乱序版本、同批联合更正与撤销。同 `batch_id` 且源字节相同只返回原 seq（`duplicate:true`）；字节不同拒绝。任何合同期分摊或引用校验失败即整批回滚，`knowledge_seq` 不变。
- `ledger-report` 先在 `known_seq` 内选每个事件最高 rev、丢弃 void，再按生效 period 截断；输出合同、两个截止、逐期本金/计息/未付利息、逐事件有效 rev/target 及利息/本金分摊、累计净收款与剩余欠款、参与批次 seq 与源 SHA。金额均为整数，有理数保持精确，复跑字节相同。

报告恒等式在每个截止校验：本金守恒 `剩余本金 = 初始本金 - 收款本金 + 冲正本金`，以及 `初始本金 + 累计计息 - 累计净收款 = 剩余本金 + 未付利息`。

完整字段、拒绝条件、状态分摊规则与设计说明见 [docs/contract.md](docs/contract.md)。
样例本金100/3期/利率1/10：p1收50、p2冲正后退本金40+利息10、最终欠款126；把p1更正为70后冲正改为退本金60+利息10、p2计息4、最终未付利息24。

## 压力现金流数据集（tranche-build）

在严格 JSON 情景下，把贷款的开期违约（含滞后回收）、期末提前结清与清仓出售逐贷款落地为资产事实，把 senior/junior 两层证券的计息与偿付落地为负债事实，并给出池级逐期资金平衡。它复用静态入口的合同校验、等额本金尾差与 nonnegative half-up 规则，计算的是合同假设而非实际收款；命令独立，不读写静态报告或 ledger 账本。

```sh
.venv/bin/loan-schedule tranche-build \
  --source fixtures/stress/scenario-cleanup.json --output run/cleanup.db
.venv/bin/loan-schedule tranche-build \
  --source fixtures/stress/scenario-hold.json --output run/hold.db
```

- 输出为独立 SQLite：源合同/退出/证券/清仓参数、`asset_facts`（逐贷款逐期，含合同本息、违约、提前结清、确认/到账/待收回收、出售本金、取消回收与售价）、`tranche_facts`（逐层逐期开期本金、应计、付息、还本、期末本金/息欠）、`pool_facts`（报价/行权、报价、覆盖缺口、准备金动用补回释放、发起人现金）以及输入 SHA，全部带主键、外键和 CHECK，零流量行不省略。
- 报价以正常当期资产流之后的剩余正常本金与未到账回收为基数（已到账回收不再出售），逐贷款 half-up 求和；只有当期现金 + 准备金 + 报价能一次清偿两层全部本息才行权，否则保留全部资产走正常瀑布。行权后取消未来合同与回收现金，后续期输出零行，禁止二次清仓。
- 发布前在引擎与 SQLite 两侧校验资金恒等式、贷款本金/回收守恒、证券滚动与清仓归零，并提供 `v_*_conservation` / `v_tranche_rollforward` 复核视图。临时库通过全部检查并关闭后才 `os.replace` 原子发布；任何失败退出 2 且旧库字节不变。
- 全程整数与 `Fraction` 精确运算，无浮点；金额为整数最小货币单位且满足有符号 64 位范围。有序 SQL 查询复跑一致，不要求数据库文件字节一致。

## 采购标段授予数据集（contract-award）

在采购批次上按“资质 + 容量”做可审计的标段授予，输出逐标段分配、每个候选报价的资格状态与未授标原因：

```sh
.venv/bin/contract-award --source fixtures/award/procurement.json --output run/awards.json
# 或
.venv/bin/loan-schedule contract-award --source fixtures/award/procurement.json --output run/awards.json
```

- 输入 `{procurement_id,lots,suppliers,bids}`：≤200 个不可拆分标段、≤100 个供应商（含 `capacity` 与 `certificates`）、≤5000 份逐标段报价（`amount` 1..10^12）。沿用既有 `ident` 规则与受限 JSON 读取器（≤8MB、拒绝重复键/非有限数/布尔冒充整数/未知或缺失字段/悬空引用/重复证书与 `(supplier,lot)`）。
- 只有供应商证书覆盖该标段全部必需证书的报价才可参与；不合格报价仍保留并列出 `missing_certificates`，但不参与授标。每标段至多一个供应商、每供应商中标数不超过 `capacity`。
- 三级目标：先最大化授标标段数，再最小化中标总价，最后取 `(lot_id,supplier_id,bid_id)` 排序序列字典序最小方案；同价必须可复现，结果不依赖输入行序。
- 输出 `schema_version:1`，含 `source_sha256`、目标摘要、逐 lot（全部候选、资格、是否中标、中标 ID 或 `no_bid`/`no_qualified_bid`/`capacity_competition` 原因）与逐 supplier（capacity、获配 lot、剩余容量）汇总；键排序、复跑字节一致，写前做结果自校验并原子替换，失败退出 2 且保留旧输出。
- `tests/test_award.py` 用独立穷举 oracle 核对小规模最优解，并覆盖资质阻断、容量全局冲突、标段唯一、同价破同分与乱序稳定。

## 电表分时电量与日费用数据集（meter-tou）

把不规则 UTC 采样区间按实际秒数分摊，裁剪到报告窗口，再按当地日界、费率边界与 UTC 偏移变化切成最大连续片段，输出逐当地日的覆盖缺口与精确分时费用：

```sh
.venv/bin/meter-tou --source fixtures/meter/sample.json --output run/meter.json
# 或
.venv/bin/loan-schedule meter-tou --source fixtures/meter/fall-back.json --output run/meter.json
```

- 输入 `{dataset_id,timezone,window_start,window_end,meters,tariffs,intervals}`：时区仅允许 `UTC`、`Asia/Shanghai`、`America/New_York`、`Europe/Berlin`，由标准库 `zoneinfo` 读取系统 IANA 数据，未知名称或数据缺失明确失败、不回退 UTC；窗口与区间为整数 Unix 秒并限 2000..2100 且 ≤31 天；1..200 个电表、1..96 档覆盖 `[0,1440)` 且无缺口的分钟费率（精确非负分数，每 Wh 最小货币单位）、0..20000 条区间；拒绝未知/缺失字段、重复键、布尔冒充整数、悬空引用、同表 UTC 区间相交与重复行。
- 电量按原始 UTC 时长均匀分摊，先裁剪再切分；回拨日重复的当地小时分别按各自偏移计量，前拨跳过的不存在时段不补量；同区间内同日/同偏移/同费率的相邻片段合并，跨区间不合并。窗口终点不属于任何片段，端点的秒数保留、不取整到分钟。
- 逐电表 × 逐当地日输出 `expected_seconds`（该日与窗口的物理秒数，自动反映 82800/90000）、`covered_seconds`、`missing_seconds` 与 `complete`/`partial`/`absent` 状态；无观测日也输出，绝不把缺口补成零电量事实。每日列出全部费率（未经者精确 0）、精确电量/费用 `{numerator,denominator}`，并在日末对精确费用只做一次非负 half-up 得 `cost_integer`；总费用为日整数费用之和，另给全窗口精确费用。
- 输出 `schema_version:1`，含 `source_sha256`、窗口、稳定排序的片段/日/电表总计；写前核验“每区间裁剪电量=分片和、日与费率汇总=片段和、covered≤expected”，原子替换，失败退出 2 且保留旧报告；乱序输入业务结果相同、同源字节报告字节一致。样例 `fixtures/meter/`，测试 `tests/test_meter_tou.py`。

## 关系表候选键画像数据集（table-keys）

对严格 JSON 关系 `{dataset_id,columns,rows}`（1..24 列、0..20000 行，单元格仅允许 null/布尔/范围内整数/≤256 字符串）在**全部列子集**上求包含极小的唯一列组（候选键）与包含极大的非唯一列组，并为每个最大非键给出可回查的字典序最小碰撞行对：

```sh
.venv/bin/table-keys --source fixtures/table_keys/parity.json --output run/keys.json
# 或
.venv/bin/loan-schedule table-keys --source fixtures/table_keys/parity.json --output run/keys.json
python -m loan_schedule.cli table-keys --source fixtures/table_keys/parity.json --output run/keys.json
```

- 比较按类型与值同时相等：`true`、`1`、`"1"` 互不相等，`null` 彼此相等且不同于字符串 `"null"`；字符串保留原始 Unicode 码点不归一化，`0` 与 `-0` 相等；`row_id` 只是元数据，不参与唯一性，完全重复值行保留并参与判定。
- 最小/最大是集合包含关系而非列数；0/1 行时唯一边界为 `[[]]`；存在全列重复行时无键且最大非键仅含全列组。等价列先折叠为类，再用“行对协议掩码”穷举（内存受限时切换为增量划分 DFS，两条路径发布前互证），最小键由最大非键补集的极小击中集对偶得到，不抽样、不截断。
- 报告含 `schema_version`、`dataset_id`、`source_sha256`、`row_count`、规范列序（Unicode 码点）、`minimal_keys`（列名数组的数组）与 `maximal_nonkeys`（每项恰含 `columns` 与 `witness=[a,b]`）；组内列名排序、边界按列数再按序列排序。写前独立核验唯一/非唯一性质、极小/极大性、互补覆盖与见证，原子替换，失败退出 2 且保留旧报告；行列置换业务结果不变，同字节输入报告字节一致。
- 规模验收（4096 行 ×24 复制位列）输出 4096 个最小键与 12 个最大非键，单 CPU/512MiB/60 秒内完成；`python tests/tools/make_scale_fixture.py --output /tmp/scale.json` 可生成该样例。样例见 `fixtures/table_keys/`，独立全格枚举 oracle 与全部边界/见证/非法输入测试见 `tests/test_table_keys.py`。

不提供计费合规认证；样例均为原创合成数据。

不提供金融建议或授标合规认证；样例均为原创合成数据。
