# Changelog

本项目的所有重要变更都记录在本文件中。格式基于
[Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

## [Unreleased]

### Planned
- M9.1 REST API 鉴权中间件（OAuth2 / API Key，二选一由用户定）
- M9.2 外部任务（external task）拉模式（对齐 Camunda 7 long polling）
- M9.3 DMN 历史审计（哪条规则在哪次 evaluate 被命中）

## [0.1.0] - 2026-09-05

> 主干里程碑全部交付的首个版本。语义对齐 Camunda 7 BPMN 引擎，Apache-2.0 独立实现。

### Added — 引擎核心（M0 ~ M4）

#### M0：骨架
- 包结构（`camunda/` + `tests/` + `examples/` + `docs/`）
- BPMN XML 解析（基于 lxml，支持子集）
- 模型层（`bpmn.py` / `dmn.py` / `execution.py` / `task.py` / `variable.py` / `job.py`）
- 公共工具（`clock.py` / `exceptions.py` / `idgen.py` / `timers.py`）

#### M1：内存版引擎流转
- `ProcessEngine` 基础接口：`deploy / start / complete / create_task_query`
- 顺序流 / 排他网关 / 并行网关 / 包容网关（基础语义）
- UserTask / ServiceTask / ScriptTask / BusinessRuleTask
- 服务任务的 delegate 注册表（`engine.register_delegate(name, fn)`）
- 表达式求值子集（`${var}`、`==`/`!=`/`>`/`<`、and/or）

#### M2：SQLAlchemy 持久化
- ACT_RE / ACT_RU / ACT_HI 三套表（Resource / Runtime / History）
- 状态映射：ProcessInstance / Execution / Task / Variable / Job
- 崩溃恢复：引擎启动时把 RUNNING 标记为 SUSPENDED，可继续
- 历史写入：完成 / 终止 / 边界事件触发时落库

#### M3：作业执行器
- Timer Start Event（流程定义级）
- Timer Intermediate / Boundary Event
- Timer 抢锁（内存版）：`_execute_due_jobs` 单线程扫描 + 执行
- asyncBefore / asyncAfter：服务任务的异步延续
- 失败重试（`retries_` 字段 + 指数退避）
- 死信处理（`retries=0` 仍可见但不被 acquire）

#### M4：完整 BPMN 事件家族

**M4-1：timer 边界事件 + asyncAfter**
- 中断式 timer 边界（cancelActivity=true）：宿主取消、跳到边界下游
- asyncAfter 完成时创建延续 job

**M4-2a：内嵌子流程**
- SubProcess 容器递归解析
- 展开（enter）与收束（leave）复活机制
- 边界 timer 中断整段 scope（scope-creating 事件）

**M4-2b：事件子流程 + 非中断式边界**
- 事件子流程的 error end 冒泡捕获（`errorRef`）
- 事件子流程的 timer start 订阅触发
- 非中断式边界事件（cancelActivity=false）：宿主不取消、spawn 并发线
- root 节点非中断订阅常驻

**M4-2c：多实例（multi-instance）**
- `MultiInstanceLoopCharacteristics`：userTask / serviceTask / subProcess 三宿主
- `completionCondition` 提前终止
- loopCounter / 元素变量（`item` / `index`）
- 持久化恢复（恢复后能数 index）

**M4-2d：消息 / 信号事件**
- `engine.correlate_message(...)` 1:1 关联
- `engine.throw_signal(...)` 跨实例广播
- catch / 边界 / 事件子流程 start / 中间 / 结束抛出 全形态
- 非中断订阅常驻
- 恢复时重推导（按未触发订阅补 spawn）

### Added — DMN（M5）

- 决策表解析（基于 lxml）
- FEEL 求值子集（`==` / `!=` / `>` / `<` / `and` / `or` / `not` / 单目）
- 6 种 hitPolicy：
  - `UNIQUE`：唯一命中
  - `FIRST`：取首条命中
  - `ANY`：短路求值（命中等价）
  - `PRIORITY`：按规则优先级排序取首
  - `RULE ORDER`：按规则物理顺序取首
  - `COLLECT`：全部命中聚合（`+` / `<` / `>`）
- `BusinessRuleTask` 集成：`camRuleDecision` / `decisionRef`
- `evaluate_decision_by_key` / `evaluate_decision_by_id`

### Added — REST API（M6）

- FastAPI 应用工厂（`create_app(engine)`）
- 前缀 `/engine-rest` 对齐 Camunda 7
- 27 个端点分 6 类：
  - Deployment（create XML / create BPMN+DMN / list / get）
  - ProcessDefinition（list / get XML / start form / start）
  - ProcessInstance（list / get / delete / variables get+set+delete）
  - Task（list / get / claim / unclaim / complete / delegate）
  - History（process-instance / task / activity-instance / variable-instance）
  - Decision（list / get XML / evaluate）
- 异常映射：5 种 Camunda 异常 → 400/404/409/500（带 `type` / `message` 错误体）
- 变量双形态：入参兼容包装形态 `{"amount": {"value": 20000}}` 与裸值 `{"amount": 20000}`；
  出参默认包装形态，带 `?bare=true` 退化为裸值

### Added — 多 JobExecutor 抢锁（M7）

- `Store` CAS 原语：
  - `acquire_due_jobs(limit, owner, lease_until)`：原子抢锁，返回 Job 列表
  - `complete_job_cas(job_id, owner)`：CAS 删除，owner 不匹配则失败
  - `reschedule_job_cas(job_id, owner, duedate, retries)`：CAS 改 due
  - `extend_lock(job_id, owner, new_lease_until)`：续约
  - `list_locks()` / `list_locks(owner=...)`：监控
- `JobExecutor` 自动分配 `lock_owner = hostname-pid-name-uuid8`
- `_execute_due_jobs_db` 路径：DB 模式下走 CAS lease
- 内存模式向后兼容（`store=None` 走旧的 `_execute_due_jobs` 路径）
- 验证：A 抢 4 条 / B 抢 1 条无重复 + A 空闲 5 轮后 B 接管 lease 过期作业

### Added — REST 列表分页（M8）

- `pagination.normalize_pagination(firstResult, maxResults)` / `paginate(items, ...)`
- 9 个列表端点统一支持 `firstResult` + `maxResults` Query 参数
- 默认 `maxResults=200`，硬上限 1000
- 非法值走 FastAPI `Query(ge=0)` 校验 → 422
- 先业务过滤后分页
- 响应仍是裸数组（对齐 Camunda 7），调用方靠「返回数 < maxResults」判末页

### Added — 文档（M9）

- `README.md`：状态盘点 + 里程碑表 + 用法
- `docs/USER_GUIDE.md`：18 章教程（1321 行），拿到代码第一份看的文档
- `docs/ARCHITECTURE.md`：设计论证 + 10 个 milestone 交付记录 + 与 Camunda 7 差异表
- `LICENSE`（Apache-2.0）
- `.github/workflows/tests.yml`：GitHub Actions CI（Python 3.11 + 3.12 双版本矩阵）
- `.github/ISSUE_TEMPLATE/{bug_report,feature_request,question}.yml`
- `.github/PULL_REQUEST_TEMPLATE.md`

### Test / Quality
- **268 单测，28 个测试文件，零回归**
- **10 个 demo 全过**：`run_demo / boundary / subprocess / ni / mi / msg_sig / dmn / api / timer / lock`
- 单测覆盖：引擎流转 / 持久化 / 作业 / 边界 / 子流程 / 事件子流程 / 多实例 / 消息信号 / DMN / REST / JobExecutor 抢锁 / 分页

### Known Limitations
- **DMN 定义不落库**：M5 主动拍板不落库（与 delegate 不入库保持一致）。如需 reload 需 `e.deploy(dmn)` 重新载入
- **REST 鉴权未实现**：明文接口，按部署环境加 OAuth2 / API Key
- **Web 控制台未实现**：用 `examples/run_api_demo.py` 看 REST 能力；用 `docs/USER_GUIDE.md` 看完整端点表
- **SQLAlchemy 2.0 only**：不兼容 1.x
- **Python 3.10+**：用了 PEP 604 union（`int | None`）

### Migration Notes
无（前 0.x 初始版本）。

[Unreleased]: https://github.com/movingheart/camunda-python/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/movingheart/camunda-python/releases/tag/v0.1.0
