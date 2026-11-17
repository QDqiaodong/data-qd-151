# data-qd-151 · 等额本金贷款计划

Exact principal amortization with period interest rounding。业务输入、计算和当前边界详见 [数据契约](docs/contract.md)。

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

src/loan_schedule/model.py 为业务模型，相关算法模块独立组织；common.py 提供受限JSON及报告发布，
cli.py 衔接数据链路。fixtures 为明确自制样例，代码与样例采用MIT许可。

## 当前能力和样例
# Equal-principal loan data schedule v1

为贷款数据分析人员生成等额本金计划。principal为1..10^12整数货币最小单位，periods=1..10000且不得超过本金最小单位数；rate_numerator=0..10^6、rate_denominator=1..10^9表示每期利率，非年利率。
前periods-1期偿还floor(principal/periods)，末期偿还全部尾差；每期开期余额乘每期利率，以非负half-up逐期舍入至整数最小单位，保留舍入前有理数。无复利、日期、提前还款、费用或逾期；不是等额本息。总本金必须恰等初始本金，末余额0。
样例本金1000/3期、利率1%，本金333/333/334，利息10/7/3，总利息20、总支付1020。未来可围绕真实日期和事件型偿还演进；当前不提供金融建议、利率合法性认证或真实放款。合成输入。
