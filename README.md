# 沿海储能柜的暴露证据与送试承诺

面向产品经理与试验站的内部工程决策应用：把柜体设计、试样暴露、停试原因与现场运行
记录放在一起，回答某一设计当前能承诺到什么使用范围，以及下一轮有限试验资源应花在哪里。

**边界：只做内部工程决策，不签发法定认证，不把加速试验小时数换算成年限，不把不同
试验协议混成一个通过率。**

## 环境

Python 3.11.2；依赖已在 requirements.txt 固定版本。

    python3 -m pip install -r requirements.txt

## 两个包的分工

- `sourcepack/`：通用资料容器检查器，核对文件摘要、字段类型、记录键、引用与行数，
  不做任何估计、推荐或批准。入口：`python3 -m sourcepack`。
- `sealpack/`：业务分析应用，通过仓库根的 `seal` 包装脚本以隔离模式运行，资料路径
  一律用 `--data` 显式传入，不从数据目录导入代码。完整使用方式见
  [docs/使用方式.md](docs/使用方式.md)，字段口径见 [docs/资料口径.md](docs/资料口径.md)。

## 常用入口

    # 可重复完整分析运行（证据单元、防偏诊断、用途评估、同环境比较）
    python3 -I seal --replicates 500 run --data /workspace

    # 承诺查询（资料用途或自定义条件）
    python3 -I seal query --data /workspace --request R1

    # 缩小环境 / 降低时长的让步情景
    python3 -I seal scenario --data /workspace --request R1 --kind narrow --drop humid,inland

    # 冻结承诺、资料变更后重评影响、显式换版
    python3 -I seal save     --data /workspace --ledger work/ledger --design D1 --conditions <条件.json>
    python3 -I seal impact   --data <资料目录>   --ledger work/ledger
    python3 -I seal supersede --data <资料目录>  --ledger work/ledger --old-commitment <旧编号> ...

    # 试验安排校验、缺口、情景、资源被占用后的重判
    python3 -I seal plan-validate --data /workspace --plan work/plans/plan_a.json
    python3 -I seal plan-gaps     --data /workspace --request R1
    python3 -I seal plan-scenario --data /workspace --plan work/plans/plan_a.json --request R1
    python3 -I seal plan-recheck  --data /workspace --plan work/plans/plan_a.json --request R1 --overrides <占用.json>

## 方法要点

- 腐蚀是区间结局（两次巡检/两个可见日之间），用非参数 Turnbull 自洽估计，不做参数
  寿命外推；右删失的安全停试、设备故障、计划结束分别保留，不合并成同类阴性。
- 不确定度用整群自助：试验室按制造批、现场按设备重采样，不把同批/同机多个体当独立样本。
- 承诺分 supported / limited / adverse / unknown 四级；“支持范围有限”与“观测到较差
  表现”严格区分；任一环境无证据时总体不得判支持，未知工况不外推。
- 现场密封换型后另起经历，后续不算原设计完整寿命；试验室小时与现场日不互相换算。
- 承诺台账只追加：资料变更后已发承诺原样保留，重评只出影响清单，显式换版才生成引用
  原编号的新快照。

## 测试

    python3 -m unittest discover -s tests -v
    python3 -m compileall -q sourcepack sealpack tests seal

- `tests/test_sources.py`：资料容器与输入诊断；
- `tests/test_survival.py`：Turnbull 区间删失、末端桶、整群自助的统计正确性；
- `tests/test_business.py`：停试/换型/区间结局口径、四级承诺、防偏、排舱硬约束、
  资源竞争、情景不预支证据、台账只追加与显式换版的端到端业务检查。

## 目录

    data/            合成业务资料（UTF-8 CSV，catalog.json 保存摘要与字段契约）
    docs/            资料口径与应用使用方式
    sourcepack/      通用资料检查器（不含业务规则）
    sealpack/        业务分析：records/survival/evidence/commitments/planning/report/cli
    seal             隔离模式命令行包装
    work/plans/      示例试验安排输入
    work/conditions* 示例客户条件输入
    work/ledger/     承诺快照台账（运行时生成，不入库）
