# data-qd-151 · 等额本金贷款计划与还款事实集市

静态等额本金计划 + 可审计的单贷款还款事实集市。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

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
sqlstore.py 管理 SQLite 事务与表结构，common.py 提供受限JSON及报告发布，cli.py 衔接数据链路。
fixtures 为明确自制样例，代码与样例采用MIT许可。

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

不提供金融建议、利率合法性认证或真实放款。合成输入。
