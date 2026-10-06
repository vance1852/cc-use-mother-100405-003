# 管控重大科技项目里程碑协作基础服务

本项目提供科技创新协作场景共用的服务端基础能力，用于登记科研机构、创新节点、操作者和结构化业务资料，并提供角色权限、请求幂等、SQLite 事务与哈希串联审计。重大项目、科研证据、成果转化和国际合作等领域可以在这些稳定边界上扩展自己的状态、规则和接口。

## 目录

- `src/science_strategy_foundation/`：领域模型、SQLite 存储、权限服务、审计链、HTTP 路由和离线验收；
- `tests/`：基础规则、事务边界、接口路由和端到端验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.acceptance
```

验收命令会在临时 SQLite 数据库中登记科研机构、操作者、创新节点和业务资料，核对幂等回执与审计链，成功时输出一行 `status` 为 `ok` 的 JSON 并以退出码 `0` 结束。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m science_strategy_foundation.api --database science_strategy.sqlite3 --host 127.0.0.1 --port 8080
```

健康检查使用 `GET /health`。写入接口通过 `X-Actor-Id` 标识操作者，服务重启后 SQLite 中的业务状态和审计历史继续保留。

## 项目里程碑与变更控制服务

在基础包之上新增 `milestone_control` 包（共用同一 SQLite 库、审计链与幂等回执，自有表统一使用 `mc_` 前缀），把专项、课题、工作包、依赖关系、指标口径、证据包、专家资格、预算分期和交付承诺固化为各自的版本；阶段门、验收结论、资金台账与变更单不可变留痕。

核心规则：

- **阶段门原子通过**：只有前置阶段门有效通过、独立验收有效、会签达到法定人数（验收责任专家不计入赞成）同时成立时，阶段门才原子通过并释放对应预算分期；任一条件不满足都返回结构化 `gate_blocked` 原因，不释放任何额度。
- **证据不可重复占用**：同一证据版本在用于资金释放的独立验收中只能占用一次；证据生产时声明的指标口径与当前版本不兼容时不得再被验收使用。一份声明多口径的证据可在同一次验收内支撑多个指标。
- **变更沿依赖传播**：路线调整、部分通过、限期整改、专家结论撤回、课题终止都生成变更单，并沿里程碑依赖范围生成后继决定（后继会签轮次、下游挂起、资金暂缓、承诺重定基线、分期作废等）。
- **可审计不回滚**：已释放/支付/关账台账与旧版决定永久保留；结论撤回只撤销尚未支付的额度，已关账支付不受影响。
- **唯一稳定结果**：所有写入走 `BEGIN IMMEDIATE` 短事务、进程内事务锁与 request_id 幂等回执，并发会签或进程恢复重放得到同一条决定、不重复释放。

管理查询：

- `GET /mc/critical-path?project_id=...`：当前关键路径（终止节点剔除、已通过节点工期归零）。
- `GET /mc/funds/pending?project_id=...`：尚未释放的分期与每笔资金被阻断的确切原因。
- `GET /mc/gates/status?gate_milestone_id=...`：阶段门状态、验收、会签、分期与阻断原因。
- `GET /mc/changes/<change_id>`：一次变化影响了哪些门、预算和交付承诺；`GET /mc/versions/<entity>/<id>` 查看固化过的全部版本。

启动（同一端口同时提供基础服务与 `/mc/*` 接口）：

```bash
PYTHONPATH=src python3 -m milestone_control.api --database milestone_control.sqlite3 --host 127.0.0.1 --port 8090
```

里程碑服务离线验收：

```bash
PYTHONPATH=src python3 -m milestone_control.acceptance
```

该命令在临时库中走完证据重复占用拦截、口径不兼容传播、原子阶段门、部分通过整改、结论撤回、课题终止、关键路径与重启幂等等场景，全部检查通过时以退出码 `0` 结束。

