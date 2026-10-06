# 管控重大科技项目里程碑协作基础服务

本项目提供科技创新协作场景共用的服务端基础能力，用于登记科研机构、创新节点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。

在这些稳定边界之上，项目进一步实现了**面向重大科技专项的里程碑与变更控制服务**：把专项、课题、工作包、依赖关系、指标口径、证据包、专家资格、预算分期与交付承诺固化到各自不可变版本；阶段门只有在前置里程碑与独立验收**同时有效**时才在单个事务内原子通过并释放对应额度；证据不能跨不兼容口径或跨门重复占用；路线调整、部分通过、限期整改、专家结论撤回与课题终止沿依赖范围生成后继决定；已关账支付与旧版结论全程保留、可审计、可回放。

## 目录

- `src/science_strategy_foundation/`
  - 基础层：领域模型（`models.py`）、SQLite 存储（`storage.py`）、权限与幂等服务（`service.py`）、审计链（`audit.py`）、HTTP 路由（`api.py`）；
  - 里程碑层：里程碑服务（`milestone_service.py`）、数据对象（`models_milestone.py`）、依赖图与关键路径（`graph.py`）、离线验收（`acceptance_milestone.py`）。
- `tests/`：基础规则、里程碑核心不变量、并发/恢复、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 核心规则

- **版本固化**：专项/课题/工作包/里程碑/依赖/口径/预算计划/承诺均为「当前版本指针 + 不可变版本行」。里程碑的验收要求绑定口径的**精确版本**；修订只追加新版本，旧版本的结论、放款与审计记录不被改写。
- **口径兼容类**：口径修订声明 `compatible_previous`。兼容修订沿用同一兼容类；不兼容修订（技术路线变更）分配新兼容类，钉住旧口径的里程碑必须修订到新口径后才能重新过门。
- **证据唯一占用**：证据按口径兼容类提交，数据库以部分唯一索引保证同一证据在任一时刻只被一个阶段门决定有效占用，因此既不能跨课题/跨门复用，也不能跨不兼容口径占用。
- **原子阶段门**：`POST /gates/decide` 在单个 `BEGIN IMMEDIATE` 事务内同时判定前置门（精确版本）与每个口径的独立有效结论（专家数量、资格有效期、独立性、证据占用），随后落决定、占证据、释放额度，三者要么全部生效要么全部回滚。
- **等比例放款**：一个里程碑多个口径时，按已满足口径数等比例释放（余数保留）；全部口径满足后补足全额。
- **变更级联**：不兼容口径修订、里程碑修订、依赖增删、结论撤回沿依赖闭包生成确定性 ID（`uuid5`）的 `impact`/`revocation` 后继决定，并冲击交付承诺；后继决定谱系存于 `decision_closure`。
- **资金安全**：撤回/失效只回收**尚未支付**的释放；已支付（尤其已关账）款项永不回收。课题终止注销未释放余额，已关账金额保留。
- **唯一稳定结果**：所有写请求以 `request_id` 幂等；门/级联决定 ID 由其依据（里程碑版本、前置、结论、证据）哈希确定；进程内写事务串行化。因此并发会签与进程恢复后都得到同一个唯一生效结果。

## 主要 HTTP 接口（均通过 `X-Actor-Id` 标识操作者，写请求需带 `request_id`）

| 类别 | 接口 |
| --- | --- |
| 版本化主体 | `POST /programs` `/programs/revise` `/projects` `/work-packages` `/milestones` `/milestones/revise` `/dependencies` `/dependencies/deprecate` |
| 指标口径 | `POST /calibers` `/calibers/revise` |
| 专家/证据/结论 | `POST /experts` `/qualifications` `/qualifications/revoke` `/evidence` `/evidence/void` `/verdicts` `/verdicts/withdraw` |
| 预算/支付/承诺 | `POST /budget-plans` `/budget-plans/revise` `/payments` `/payments/close` `/commitments` |
| 阶段门/整改/终止 | `POST /gates/decide` `/rectifications` `/projects/terminate` |
| 管理视图 | `GET /gates/evaluate?milestone_id=`、`GET /projects/critical-path?project_id=`、`GET /projects/funds?project_id=`、`GET /changes/impact?decision_id=`、`GET /decisions/<id>?with_descendants=1` |

`GET /projects/funds` 对每笔分期返回金额、已释放/在途余额与**资金尚未释放的确切阻塞原因**（前置未过门、结论不足、证据被占、口径已不兼容等）；`GET /projects/critical-path` 返回关键路径、各节点最早完成工期、是否在关键路径上及阻塞码。

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

基础服务：

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.acceptance
```

里程碑与变更控制：

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.acceptance_milestone
```

验收命令在临时 SQLite 数据库中登记机构、操作者、专家资格，建立专项→课题→工作包→里程碑→依赖与预算，演练独立验收、原子过门与等比放款、证据唯一占用、限期整改、不兼容口径修订的依赖级联与承诺冲击、结论撤回对未支付款的回收和已关账款项的保留、课题终止注销余额，最后核对关键路径、决定谱系与审计哈希链；成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.api --database science_strategy.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态、版本历史、决定谱系和审计历史继续保留。
