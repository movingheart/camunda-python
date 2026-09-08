# 架构

本文档解释 `camunda-python` 的模块划分、关键设计决策与「与 Camunda 7 的差异」。第一次
接触建议从 [README.md](README.md) 与 [USER_GUIDE.md](USER_GUIDE.md) 开始。

## 1. 设计目标

- **语义对齐 Camunda 7**：核心 BPMN 元素行为、状态机与持久化表结构保持一致，便于
  已有 Camunda 经验的人快速理解。
- **嵌入式库形态**：`ProcessEngine()` 构造即 ready，无后台线程（除显式启动
  `JobExecutor`），适合嵌入到既有 Python 服务。
- **可恢复性优先**：每个用户命令边界全量同步 ACT 表；崩溃后 `from_database()` 一行
  恢复执行流、变量、待办与作业。
- **跨方言持久化**：SQLite（开发）/ PostgreSQL / MySQL 三方言一致行为；大文本列
  （BPMN XML、变量 JSON）自动映射 `MEDIUMTEXT` 等方言最优类型。

## 2. 模块划分

```
camunda/
├── model/        # 纯数据模型：BpmnModel / Process / FlowNode / Execution / Task / Variable / Job
├── parser/       # lxml 自研 BPMN 2.0 / DMN 解析器
├── engine/       # 引擎门面 + 节点行为（serviceTask / gateway / event / subProcess…）
├── job/          # JobExecutor（Timer Start / Timer Catch / async continuation 推进）
├── persistence/  # SQLAlchemy 2.0 ACT 表（RE / RU / HI 三套）+ Store CAS lease 原语
├── api/          # FastAPI REST 兼容层（`/engine-rest` 前缀）
├── dmn/          # DMN 决策引擎（解析器 + FEEL 子集 + hitPolicy 收敛 + 集成）
└── common/       # 异常层次、ID 生成器、可注入时钟
```

### 2.1 引擎入口

`ProcessEngine` 是门面，聚合四类服务：

| 服务 | 关键方法 |
|---|---|
| RepositoryService | `deploy()` / `list_deployments()` / `get_process_definition()` |
| RuntimeService | `start_process_instance_by_key()` / `correlate_message()` / `throw_signal()` / `delete_process_instance()` |
| TaskService | `create_task_query()` / `claim_task()` / `complete_task()` / `set_variable()` |
| HistoryService | `list_process_instances()` / `list_tasks()` / `list_activity_instances()` |

### 2.2 持久化契约

Camunda 7 用三套 ACT 表区分**静态定义 / 运行时瞬时态 / 历史归档**，本项目保留此契约：

- `ACT_RE_*`：部署、流程定义（`ACT_RE_DEPLOYMENT` / `ACT_RE_PROCDEF`）
- `ACT_RU_*`：运行时 execution 树、任务、变量、作业、事件订阅
- `ACT_HI_*`：历史实例、活动、任务、变量

**每用户命令边界**（`deploy` / `start_process_instance` / `complete_task`）全量同步：
delete + insert 覆写 RU 快照；HI 表 `upsert`。崩溃发生在命令中途 => 该命令整体丢失，
等价 Camunda 单命令事务。

## 3. 关键设计决策

### 3.1 嵌入式 vs 服务化

`ProcessEngine()` 构造即 ready，无全局单例、无后台线程；`JobExecutor` 由用户显式
`start()`。好处：测试好隔离（每个测试一个 engine）；嵌入既有服务无需操心线程生命周期。

### 3.2 同步驱动 + 可注入时钟

引擎内部所有时间取 `clock.now()`（默认 `datetime.now`）。测试时可注入假时钟，单步
`tick()` 推进时间——无需 `time.sleep`，测试在毫秒内跑完。

### 3.3 订阅派生态（不落库）

消息 / 信号事件的订阅表 `engine._event_subs` 是**纯内存结构**，不写入 `ACT_RU_*`。
崩溃恢复时从 execution 树静态推导重建（`_rebuild_event_subscriptions`），与
`join_arrivals` 等待登记的恢复路径同构。

### 3.4 多进程作业抢锁（CAS lease）

`Store.acquire_due_jobs` 三步式抢锁（先 `SELECT` 候选 ID，再逐条 `UPDATE ... WHERE
LOCK_EXP_TIME_ < now`，再 `SELECT` 抢到的详情），跨方言一致。长作业通过
`extend_lock` 续约；非 owner 调用 `complete_job_cas` / `reschedule_job_cas` 返回
`False`，防御性拒绝。

### 3.5 DMN 独立可脱离 BPMN 使用

`DmnEngine` 是独立模块，可单独 `deploy` + `evaluate_decision`，不依赖 BPMN 引擎；
通过 `ProcessEngine.deploy_dmn` 集成后，BPMN `businessRuleTask` 节点会同步求值并写
入 `resultVariable`。

### 3.6 FastAPI 字符串注解坑

`from __future__ import annotations` 下的路由函数签名是字符串，必须能在模块 globals
中解析。漏 `import Optional` 会让 FastAPI 静默丢掉 body 参数（看似请求成功但变量
全为 `None`）。所有 router 模块用 `typing.get_type_hints(fn)` 自检通过。

## 4. 与 Camunda 7 的差异

| 维度 | Camunda 7 | camunda-python |
|---|---|---|
| **运行形态** | 独立服务（war/jar）+ Web 控制台 | 嵌入式 Python 库（无独立服务进程、无 Web 控制台） |
| **持久化策略** | 逐行 update | 命令边界 delete + insert 全量重写（中小流程足够快） |
| **变量作用域** | execution 局部（可子作用域遮蔽） | 实例级单一作用域（无局部遮蔽） |
| **DMN 决策形态** | decisionTable / literalExpression / relation / invocation / context | 仅 decisionTable（其余部署期报错） |
| **FEEL 支持** | FEEL 全量（含函数 / 日期时间 / 路径） | 子集：比较 / 区间 / 列表 / `not` / `null` / 算术 / 字符串拼接 |
| **DMN 持久化** | `ACT_RE_DECDEF` 落库 | **不落库**，崩溃恢复后需重新 `deploy_dmn` |
| **多进程抢锁** | `SELECT FOR UPDATE` 行锁 | 应用层 `UPDATE` CAS（跨 SQLite / PG / MySQL 一致） |
| **订阅持久化** | `ACT_RU_EVENT_SUBSCR` 落库 | 纯内存派生态，恢复从 execution 树推导 |
| **REST 鉴权** | basic auth / JWT | 不支持（引擎无 identity 模块） |
| **删除实例历史** | 可选 `skipHistory` | 固定保留（`HI_PROCINST` 置 `DELETED`） |
| **Cockpit / Tasklist** | Web 控制台 | **不做** |
| **External Task** | 支持 | 不做 |

### 4.1 持久化方言支持矩阵

| 方言 | 状态 | 大文本列 |
|---|---|---|
| SQLite | ✅ 默认 dev 方言 | `TEXT` |
| PostgreSQL | ✅ prod 推荐 | `TEXT` |
| MySQL | ✅ prod 可用 | `MEDIUMTEXT`（16MB，避免 `TEXT` 64KB 上限） |

MySQL 旧库无 `MEDIUMTEXT` 列时，需要 `ALTER TABLE` 或重建；`scripts/verify_mysql_compat.py`
提供 `--local`（SQLite 冒烟）与默认（真实 MySQL 全链路验证）两种模式。

## 5. 测试

`pytest` 一键跑全套：

- 解析层（每个 BPMN / DMN 元素单独测试）
- 引擎流转（无 Store 内存模式）
- 持久化（SQLite 模式）+ 崩溃恢复（`from_database` 路径）
- JobExecutor（注入时钟的单步 tick）
- REST 端点（FastAPI `TestClient`，含异常映射与分页）
- 多进程抢锁（双 JobExecutor 共享 store，验证不重复执行）

CI 在 GitHub Actions 上跑 Python 3.12 / 3.13。