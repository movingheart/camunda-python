# Camunda 7 → Python3 重写 · 架构规划

> 状态：v0.2 已定稿（老板拍板 2026-09-02）；M0/M1/M2/M3/M4-1/M4-2a/M4-2b 已交付（2026-09-02）
> 参考基线：Camunda 7.23.0（社区版最后开源稳定版，Apache-2.0，2025-10 停止社区维护）
> 技术选型结论：语义对齐为主 · SQLAlchemy 2.0 · lxml 自研解析 · M0 骨架 + M1 内存流转 + M2 持久化已交付

---

## 1. 重写目标

| 维度 | 说明 |
|---|---|
| 语言 | Python 3.12+（类型标注、dataclass、async 可选） |
| 定位 | 复刻 Camunda 7 核心引擎语义：**BPMN 流转 / 事务持久化 / 作业调度** |
| 兼容策略 | 先做**语义兼容**（流程能跑对、状态能存能恢复），REST API 兼容层后置 |
| 不做（初期） | Cockpit / Tasklist Web 控制台、DMN 引擎可后置为独立里程碑 |
| 许可考虑 | 新代码 Apache-2.0 独立实现，不直接搬运 Java 源码 |

## 2. Camunda 7 核心架构 → Python 模块映射

Camunda 7 内部按 Command 模式组织：`CommandExecutor → CommandContext → DbEntityManager(MyBatis) → DB`，外加 `JobExecutor` 扫描 `ACT_RU_JOB` 表异步推进定时器/异步延续。

| Camunda 7 Java 组件 | 职责 | Python 对应包 |
|---|---|---|
| `bpmn-model` | BPMN 2.0 XML 解析 + 模型对象（`BpmnModelInstance`） | `camunda.parser` + `camunda.model` |
| `engine-core` 行为层 | 元素行为（`ActivityBehavior`）：服务任务/网关/事件/子流程流转逻辑 | `camunda.engine.behavior` |
| `ProcessEngineImpl` | 引擎门面（`RepositoryService` / `RuntimeService` / `TaskService`…） | `camunda.engine` |
| Command/Interceptor 栈 | 事务边界、权限、日志拦截 | `camunda.engine.command` |
| `JobExecutor` | 定时器 / 异步延续执行，DB 轮询式调度 | `camunda.job` |
| MyBatis 持久层 | `ACT_RE_*`（定义）/ `ACT_RU_*`（运行）/ `ACT_HI_*`（历史）三套表 | `camunda.persistence` |
| 事件机制 | ExecutionListener / TaskListener / 流程事件广播 | `camunda.engine.event` |
| DMN 引擎 | 决策表求值（FEEL 表达式） | `camunda.dmn`（M5） |
| REST API / Webapps | 远程接口与运维界面 | `camunda.api`（M6 起） |

### 核心数据表（持久化契约，重写必须对齐）

Camunda 用三套 ACT 表区分**静态定义 / 运行时瞬时态 / 历史归档**，重写时保留此契约：

- `ACT_RE_PROCESS_DEF`、`ACT_RE_DEPLOYMENT`、`ACT_RE_PROCDEF`
- `ACT_RU_EXECUTION`（树形父子 execution，token 的载体）
- `ACT_RU_TASK`、`ACT_RU_VARIABLE`、`ACT_RU_JOB`、`ACT_RU_EVENT_SUBSCR`
- `ACT_HI_PROCINST`、`ACT_HI_ACTINST`、`ACT_HI_TASKINST`、`ACT_HI_VARINST`

## 3. Python 侧目录结构

```
CodeSpace/camunda/
├── pyproject.toml            # 工程配置（打包、依赖、工具链）
├── camunda/
│   ├── __init__.py           # 版本号、引擎入口 re-export
│   ├── model/                # 纯数据模型（不依赖 DB）
│   │   ├── bpmn.py           # BpmnModelInstance 等价物：流程/节点/连线/事件对象
│   │   ├── execution.py      # Execution / ProcessInstance / ActivityInstance 视图
│   │   ├── task.py           # 用户任务/任务查询结果
│   │   ├── variable.py       # 变量类型体系（Java 类型映射）
│   │   └── job.py            # Job 定义
│   ├── parser/               # BPMN 2.0 XML → model
│   │   ├── __init__.py
│   │   └── bpmn_parser.py    # 基于 lxml / xml.etree 的解析器 + 扩展元素(extensionElements)支持
│   ├── engine/               # 引擎核心
│   │   ├── __init__.py
│   │   ├── process_engine.py # ProcessEngine 门面（各 Service 聚合）
│   │   ├── services.py       # RepositoryService / RuntimeService / TaskService / HistoryService
│   │   ├── behavior/         # 节点行为：顺序流/排他网关/并行网关/服务任务/子流程/事件
│   │   ├── command/          # Command 模式 + 事务拦截器（对齐 Java 架构）
│   │   ├── event.py          # 监听器与事件发布
│   │   └── expression.py     # 表达式求值（${...} 简化为 Python 表达式安全子集）
│   ├── job/                  # 作业执行器（独立线程/进程轮询 ACT_RU_JOB）
│   ├── persistence/          # 持久层
│   │   ├── schema.sql        # 建表 DDL（SQLite/PostgreSQL 双方言）
│   │   ├── entities.py       # ORM 实体（SQLAlchemy 或轻量 DAO）
│   │   └── repositories.py   # DbEntityManager 等价物：按 ID 增删改查 + 锁
│   ├── api/                  # ✅ M6 REST 兼容层：app 工厂 / errors / schemas / deps / routers
│   ├── dmn/                  # ✅ M5 DMN 决策引擎：model / parser / feel / engine
│   ├── common/               # 异常层次、工具（IdGenerator、Clock）
│   └── conf.py               # 引擎配置（对齐 camunda.cfg.xml 配置项）
├── docs/
│   ├── ARCHITECTURE.md       # 本文档
│   └── milestones.md         # 里程碑与验收标准
├── examples/                 # 可运行的 BPMN 示例流程
└── tests/
    ├── unit/                 # 单测
    └── integration/          # 端到端：部署→启动→流转→持久化恢复
```

## 4. 里程碑（增量交付）

| 里程碑 | 内容 | 验收标准 |
|---|---|---|
| **M0** | 工程骨架 + 模型层 + BPMN XML 解析器 | ✅ 已交付：能解析 BPMN 样例，输出节点/连线/网关图 |
| **M1** | 内存版引擎核心流转 | ✅ 已交付：部署→启动→经过网关/任务→结束；排他/并行网关 fork-join 单测 + demo 验证 |
| **M2** | 持久化 + 事务 | ✅ 已交付：状态落 SQLite（ACT_RE/RU/HI）；进程中断后可恢复；变量读写；历史表写入 |
| **M3** | 作业执行器 | ✅ 已交付：Timer Start / Timer Catch / asyncBefore 拆分、失败重试、ACT_RU_JOB 轮询调度 |
| **M4** | BPMN 全元素补齐 | ✅ M4-1（边界事件 timer 中断式 + asyncAfter）；✅ M4-2a（内嵌子流程展开/收束 + 边界 timer 中断 scope）；✅ M4-2b（事件子流程 error/timer start + 非中断式边界 cancelActivity=false）；✅ M4-2c（多实例：userTask/serviceTask/subProcess 三宿主 + completionCondition + 持久化恢复）；✅ M4-2d（消息/信号事件：correlate_message 1:1 关联 + throw_signal 广播 + 全形态订阅 + 恢复重推导） |
| **M5** | DMN 决策引擎 + FEEL 子集 | ✅ 已交付：决策表解析 + FEEL 子集求值（比较/区间/OR/not/null）+ 六种 hitPolicy 收敛 + businessRuleTask 集成 |
| **M6** | REST API（FastAPI）+ 可选 Web 简易控制台 | ✅ 已交付：六类端点（部署/流程定义/流程实例/任务/历史/决策）+ 变量双形态 + 异常映射；Web 控制台仍属「不做」范围 |
| **M7** | 多 JobExecutor 抢锁（跨进程 DB CAS lease） | ✅ 已交付：store CAS 原语（acquire_due_jobs / complete_job_cas / reschedule_job_cas / extend_lock）+ 引擎 _execute_due_jobs_db 路径 + JobExecutor 自动分配 lock_owner + list_locks 监控 + lease 过期自动恢复 |
| **M8** | REST 列表分页（firstResult + maxResults） | ✅ 已交付：9 个列表端点统一分页 helper + 默认 maxResults=200 / 硬上限 1000 + 27 条单测覆盖 |

> 本轮任务目标：**M0~M7 已交付**（骨架 + 模型 + 解析器 + 内存流转 + SQLAlchemy 持久化与崩溃恢复 +
> 作业执行器 Timer/async/重试 + timer 边界事件与 asyncAfter + 内嵌子流程容器语义 + 事件子流程
> error/timer start + 非中断式边界 cancelActivity=false + 多实例三宿主语义与持久化 + 消息/信号
> 事件全形态订阅与恢复重推导 + DMN 决策表与 FEEL 子集求值 + FastAPI REST 兼容层 +
> 多 JobExecutor DB CAS lease 抢锁）。下轮切片按老板指令推进（候选：DMN 定义持久化、
> REST 分页与鉴权、Web 控制台）。

### M4-2c 交付记录（2026-09-03）：多实例（MultiInstanceLoopCharacteristics）三宿主语义

**范围决策（老板拍板）**：纯 MI 语义三宿主——仅 userTask / serviceTask / subProcess 可作为
MI host；**subProcess host 纳入 MI 范围**（即 subProcess 内部整体可被多实例循环驱动）。

**切片说明**：M4-2c1 = 模型 + 解析器（MI 特征解析 + 宿主白名单校验）；M4-2c2 = 引擎 userTask
宿主（任务并行/顺序 + 实例收束 + completionCondition）；M4-2c3 = 引擎 serviceTask / subProcess
宿主；M4-2c4 = 持久化与崩溃恢复验证；M4-2c5 = demo 自验证。

**模型与解析（M4-2c1）**：`MultiInstance` 携带 `sequential / collection_expr /
element_variable / cardinality_expr / completion_condition`；`camunda:collection` 与
`loopCardinality` 至少其一（同时提供 collection 优先），`elementVariable` 仅配 collection，
否则部署报错。宿主白名单在解析器落实：非 userTask / serviceTask / subProcess 的节点挂
multiInstanceLoopCharacteristics 部署即报错（文档化差异白名单）。subProcess 容器递归后同样
可挂（内部节点独立解析）。

**引擎（M4-2c2/c3）**：
- **容器形态**：顺序 = token 自身兼 MI 容器（`mi={"sequential":True,...}`，进入宿主行为不
  SCOPE 化）；并行 = token 转 SCOPE 容器 + spawn N 条 child（各带 `mi={"index": i}`）。
- **userTask 宿主**：`_start_mi_instance` 注入 loopCounter / elementVariable 后
  `_enter_user_task_wait` 建任务停等；`complete_task` 命中宿主即走 `_complete_mi_instance`
  实例完成路径（顺序续跑下一实例 / 并行 child 收束 + 条件检查）。
- **serviceTask 宿主（同步）**：无等待窗口——delegate 执行完即结算实例。顺序容器就地
  while 循环跑完剩余实例（避免 N 层嵌套 pump）；并行 child 跑完立即完成回报。start 返回时
  同步宿主已全部收束离开（无中间态）。
- **subProcess 宿主**：`_enter_subprocess` 进内部流转，等待窗口 = 整段内部执行；
  `_collapse_scopes` 的 SubProcess 分支挂 MI 钩子——内部收束后若宿主是 MI（`_mi_scope_of`
  命中）走实例完成路径（计数/续跑下一实例/条件/收束），否则照旧复活沿出边走。
- **`_container_of` 透明化（关键修复）**：并行 MI 容器停驻在宿主 subProcess 节点上，但容器
  自身并不进入子流程（进入的是其 child 实例）——它不是执行体，沿链跳过；其余
  SCOPE@sub（常规/顺序容器/并行实例载体 `mi={"index"}`）都是执行体取 inner。首版谓词误把
  实例载体也跳过，导致内部 startEvent 出边按根容器查流报 KeyError，已修正为只跳并行容器
  （`total` 键 + 非 sequential）。
- **事件回传约定（重构）**：`_start_mi_instance` / `_complete_mi_instance` /
  `_finish_mi_container` 一律返回 `List[_Arrival]`，由最外层调用点（complete_task / collapse /
  spawn 循环）统一 pump——避免顺序 subProcess 逐实例续跑时的嵌套 pump 递归加深。
- **completionCondition**：求值环境 = 实例变量 + 内置计数器 `nrOfInstances /
  nrOfActiveInstances / nrOfCompletedInstances`。并行满足条件 -> 剩余活跃实例被终止
  （`_kill_mi_active_children`：回落 active 计数、走 `_kill_execution_tree` 整树清理——
  任务归档带 end_time、open actinst 结算、job 删除、join_arrivals 摘除）；顺序满足条件 ->
  不再启动下一实例。终止 = 取消而非完成（不动 completed）。
- **变量生命周期**：loopCounter / elementVariable 行为期注入实例变量表（行为期可读），
  容器收尾 `_cleanup_mi_vars` 统一清理（不落 HI_VARINST，文档化差异）。
- **空集合**：零实例，宿主直接通过（无任务、无残留 MI 容器、无孤儿 execution）。
- **范围防御**：MI 宿主组合 asyncBefore / 边界事件 -> 运行时明确报错（纯 MI 范围，M4-2c
  暂不组合 async/事件）。
- **收束链完备性**：`_kill_execution_tree` 作为统一整树清理原语（自底向上杀灭），
  `_kill_mi_active_children` 与 `_kill_subprocess_scope` 均复用，启动失败/条件终止/边界中断
  全部收敛路径不留 ACTIVE 孤儿。

**持久化与崩溃恢复（M4-2c4）**：`ExecutionSnap` 加 `mi` 字段、`ExecutionEntity` 加 `MI_`
Text 列（可空，非 MI 执行写 NULL）——容器 dict 与实例 `{"index": i}` 以 JSON 落库/读回。
恢复路径零改动即覆盖 subProcess host 树形（`_container_of` 透明化 + open_activity 挂回 +
`_mi_scope_of` 判定已够用）。设计结论：serviceTask 宿主同步执行无停等窗口，MI 中间态
永不出现在 RU 快照（列扩展对它属纯前瞻）；loopCounter/elementVariable 随 pi.variables 全量
落库，崩溃还原值天然 = 当前等待实例序号，与容器 next_index 自洽。

**M4-2c 文档化差异**（与 Camunda 7 差异）：
| 差异点 | Camunda 7 | camunda-python M4-2c |
|---|---|---|
| MI host 范围 | 任意活动/子流程/网关等 | userTask / serviceTask / subProcess 白名单（解析器部署报错） |
| 宿主组合 asyncBefore / 边界事件 | 支持（async 前拆分 / 边界触发逐实例） | 运行时明确报错（纯 MI 范围） |
| loopCounter / elementVariable 生命周期 | 实例执行期变量（可读可写） | 行为期注入实例变量表、容器收尾统一清理（不落 HI_VARINST） |
| 变量作用域 | 实例可有独立变量作用域 | 沿用实例级单一作用域（对齐 M4-2a 文档化差异） |
| MI 计数内置变量 | nrOfInstances / nrOfActiveInstances / nrOfCompletedInstances | 同（completionCondition 求值环境提供） |
| 实例终止 vs 取消 | 条件满足剩余实例取消 | 同（回落 active、不动 completed、任务归档带 end_time） |
| subProcess 宿主内部并发 | 内部任意并行 | 沿用 M1/M4-2a 并行约束（容器内不嵌套并行网关） |
| 空集合 | 零实例直接通过 | 同 |

**验证**：M4-2c 引擎/持久化测试 20 条新增（userTask 宿主 8 + serviceTask/subProcess 宿主 7 +
持久化恢复 5，加解析层 #41 若干），全量回归 **136 passed**（M4-2c3 完成时基线 131，M4-2c4
+5 后 136，零回归）；examples/run_mi_demo.py 三场景（serviceTask 同步推送 / subProcess 顺序
逐店 / subProcess 并行 + 条件抢跑终止）纯同步驱动演示通过。

### M4-2d 交付记录（2026-09-03）：消息（message）/ 信号（signal）事件

**切片说明**：M4-2d1 = 模型 + 解析器（signal 声明贯通、message/signal 事件槽、新增
IntermediateThrowEvent 节点）；M4-2d2 = 引擎订阅基础设施 + `correlate_message` 1:1 关联；
M4-2d3 = `throw_signal` 广播 + 实例内 throw（中间/结束抛出事件）；M4-2d4 = 订阅恢复重推导
（纯内存派生态，不落库）；M4-2d5 = demo 自验证 + 文档。

**模型与解析（M4-2d1）**：
- 事件槽扩为 **4 元互斥**：timer / error / message / signal（`_parse_event_definitions`
  返回 4 元组；timer throw 部署即报错）。解析器收集 `<signal id name>`（name 回退 id），
  `signal_by_id` 贯通 process/subProcess 递归全链路。
- 节点 × 事件支持矩阵：StartEvent（事件子流程 message/signal start，中断/非中断均合法；
  **流程级 message/signal start 不落地**——手动启动明确报错，属文档化差异）；IntermediateCatchEvent
  （timer/message/signal 停等）；BoundaryEvent（timer/message/signal，中断 + 非中断）；
  EndEvent / **IntermediateThrowEvent（新节点）**（message/signal throw，error throw 沿用
  M4-2b，timer throw 拒绝）。

**引擎（M4-2d2/d3）**：
- **EventSubscription 订阅模型**（核心派生态）：`kind("message"|"signal") / event_name /
  process_instance_id / execution_id / activity_id(esc 订阅容器 sub id，None=流程级) /
  node_kind("start"|"catch"|"boundary") / is_interrupting`；注册表 `engine._event_subs`
  （插入序 = 注册序）。**纯内存派生态、不落库**（对齐 join_arrivals 先例），恢复靠重推导。
- **公共 API**：`correlate_message(name, process_instance_id=None, variables=None)` —— 1:1
  点对点，未限定实例取注册序最早；指定实例则实例内匹配；无订阅 `NotFoundException`。
  `throw_signal(name, variables=None) -> int` —— 跨实例广播全部命中订阅，返回命中数
  （实例内 signal throw 只广播本实例，跨实例归公共 API，文档化差异）。
- **触发分派（_fire_subscription -> node_kind）**：catch = 消费订阅 + token 续走；
  boundary-中断式 = 复用 `_cancel_host_activity`（撤宿主全部边界订阅含触发者）+ 沿边界出边；
  esc-start 中断式 = 流程级 `_interrupt_instance` / sub 级 `_kill_subprocess_scope` +
  撤订阅容器全部 esc 订阅（timer job + msg/sig sub）——与 `_fire_timer_event_start` 同构。
- **throw 分派**：EndEvent message/signal = token 结束同时投递（存活守卫再 `_end_token`）；
  IntermediateThrowEvent = open/close actinst + `_throw_event_in_instance`（message 1:1
  就近 / signal 实例内广播）+ 守卫再 `_leave`。**实例内 throw 未命中订阅静默丢弃**（对齐
  Camunda），命中则触发同实例 catch（并行分支协作场景）。
- **订阅生命周期**：scope 激活即注册（`_start_process` / `_enter_subprocess` /
  `_start_event_subprocess` 三处注册点，幂等去重）；触发即消费（catch / 中断式 boundary /
  中断式 esc-start）；**非中断式 boundary 与 esc-start 订阅常驻可再触发**（与 timer 单发
  的差异，文档化）；宿主离开/杀灭/收束时撤销——清理钩子铺满 `_throw_error` /
  `_cancel_host_activity` / collapse SubProcess 分支 / `_interrupt_instance` /
  `_kill_execution_tree`（kill() 逐节点撤订阅）/ `_drop_boundary_jobs` /
  `_drop_scope_event_subscriptions`，触发路径另加过期订阅惰性回收兜底。
- **既有隐患顺手加固**：`_register_event_subprocess_timers` 补幂等去重（顺序 MI 宿主续跑
  重复注册 timer esc job 的隐患，与订阅注册同一 (execution, activity_id, node_id) 键）。

**恢复重推导（M4-2d4）**：`_restore_instance` 尾部调 `_rebuild_event_subscriptions(pi)`，
从 execution 树重推导全部订阅（对齐 join_arrivals 先例，零表改动）：root 未完成 → 流程级
esc 订阅；停驻 SubProcess 的 SCOPE（actinst open）→ 容器级 esc 订阅 + embedded sub 边界
重放（事件子流程 scope 不重放边界，与运行期一致）；停等 catch token → catch 订阅；
userTask / asyncBefore 宿主 → 边界订阅重放（timer job 已随 RU 快照还原，幂等检查防重复）。
测试覆盖：catch 停等崩溃 → 重启 correlate；esc message start 恢复后关联接管；signal 边界
停驻恢复后广播（常驻可再触发）。

**M4-2d 文档化差异**（与 Camunda 7 差异）：
| 差异点 | Camunda 7 | camunda-python M4-2d |
|---|---|---|
| 消息多实例消歧 | businessKey / correlation key | 未限定实例取注册序最早（1:1 语义） |
| 流程级 message/signal start（Event-based Gateway 等） | 支持事件网关择路 | 不落地：事件子流程 start 支持，流程级 start 手动启动明确报错 |
| 实例内 signal throw 范围 | 实例内（跨实例靠 API） | 同（跨实例由 throw_signal 公共 API 提供） |
| 实例内 message throw 未命中 | 静默丢弃 | 同 |
| 非中断订阅复触发 | 边界常驻可再触发 | 同，且 esc-start 非中断同样常驻（timer esc 单发） |
| 订阅持久化 | ACT_RU_EVENT_RES_JOB 落库 | 纯内存派生态，恢复从 execution 树重推导（零表改动） |
| 实例内 throw 作用域 | 同实例 | 同（throw 事件只作用于本实例，跨实例用公共 API） |

**验证**：M4-2d 测试 22 条新增（解析 7 + 引擎 12 + 持久化恢复 3），全量回归 **158 passed**
（M4-2c 基线 136，零回归）；examples/run_msg_sig_demo.py 三场景（跨实例消息接力 1:1 精准
投递 / 信号广播 3 实例非中断并发线 / 支付停等崩溃恢复重推导续跑）演示通过。


**切片说明**：M4-2b = 事件驱动子流程族。M4-2b1/b2 = 事件子流程解析校验 + error end 冒泡捕获
（error start，中断式）；M4-2b3 = timer start 事件子流程（订阅/到期/中断/非中断）；M4-2b4 =
**非中断式边界事件（cancelActivity=false）**——事件子流程并发收束语义的地基；M4-2b5 = 持久化
与崩溃恢复验证。

**事件子流程（M4-2b1~b3，triggeredByEvent）**：
- 解析：subProcess `triggeredByEvent="true"` 与内嵌子流程同容器递归，startEvent 校验宽容；
  运行时宿主 scope 激活即订阅（中断式/非中断式事件子流程，error start 由 `_throw_error`
  冒泡匹配、timer start 注册 timer-event-start Job 单发）。
- 触发：error endEvent 抛错沿父链冒泡 -> 命中宿主 scope 的 error start 事件子流程 ->
  **中断式**清空宿主当前子树、spawn 事件子流程 scope（收束后宿主 scope 结束）；timer start
  订阅随宿主 scope 结束/触发撤销，以 (execution_id=host_scope, activity_id=sub, node_id=start)
  唯一定位。非中断式事件子流程与宿主并存，宿主收束等待子收束（root 停驻语义）。

**非中断式边界事件（M4-2b4，cancelActivity=false）**：
- `_register_boundary_jobs` 放开普通等待宿主（userTask / asyncBefore）：NI 合法注册；
  **subProcess 宿主 + NI 仍明确报错**（文档化差异：并发线需脱离 sub 容器挂父 scope，root
  兼任载体存在容器推导歧义，暂缓）。
- 到期触发（`_fire_timer_boundary` 分流 cancel_activity）：中断式走原取消路径；非中断式调
  `_spawn_non_interrupting_boundary` —— **宿主不取消**（task/actinst/其余边界作业保留），仅
  消费当前该单发 timer job，spawn 并发线（挂宿主直接父 scope；进程级直通宿主无父 -> 挂 root）
  从边界事件出边推进（边界事件在并发线上留 actinst 痕迹）。
- **实例完成语义修正（关键）**：root 到 end 时若存在活跃子执行（NI 并发线 / 非中断事件
  子流程），root **不结束** -> 转 role=SCOPE、activity_id=None 停驻等待；全部子执行收束后由
  `_collapse_scopes` 收尾段将 root 置 ENDED 并完成实例（修复"主线先完/并发线后收"被提前
  `_complete_instance` 的 bug）。收尾段 root 先置 ENDED：避免 ACTIVE root 残行被快照写回 RU。
- 完成语义自底向上：subProcess scope 内并发线（NI 触发产物）须全部收束后，sub server 才可
  复活沿出边走——内部主线走完不提前放行。

**M4-2b 持久化与崩溃恢复（M4-2b5）**：NI timer-boundary job 随实例 RU 快照透传，恢复后到期
触发走 NI 分流（host 保留 + 并发线 spawn）；root 转 SCOPE 停驻态（activity_id=None）随
execution role/activity 列还原，并发线完成后由收尾段完成。修复一个跨重启历史归档缺陷：
**HI_TASKINST 全量重写语义下，`from_database` 需把已归档任务（end_time 非空）还原回
`completed_tasks`**，否则重启后下一次 save 会抹掉重启前已归档任务历史（load_active_instances
补读 HI_TASKINST 归档行 -> `_restore_instance` 挂回）。
无需表结构改动。测试 104 全绿（+7 NI 语义 + 3 NI 持久化恢复）；
examples/run_ni_demo.py 真实时钟 + JobExecutor 三场景演示通过（正常完成边界撤销 / 超时催办
宿主保留 / 并发复核收束，约 6 秒）。

**M4-2b 文档化差异**（与 Camunda 7 差异）：
| 差异点 | Camunda 7 | camunda-python M4-2b |
|---|---|---|
| subProcess 宿主非中断式边界 | 支持（并发子分支） | 明确报错（普通等待活动宿主支持；sub 宿主容器推导歧义暂缓） |
| 非中断式边界宿主范围 | 任意活动 | userTask / asyncBefore 等待活动宿主 |
| 事件子流程 error/message/timer start | error/message/timer/信号 | timer/error 支持（M4-2b3/b2）；message/信号 ✅ **M4-2d 已交付**（见 M4-2d 记录） |
| 多实例 | 支持 | ✅ **M4-2c 已交付**（三宿主 + 持久化，见 M4-2c 记录） |
| 多 JobExecutor 抢锁 | DB 行锁 + Acquisition Row Lock | ✅ **M7 已交付**（CAS lease 简化版，见 M7 记录） |

### M7 交付记录（2026-09-05）：多 JobExecutor 抢锁（DB CAS lease）

**问题（M3 留下的文档化差异）**：单进程内多个 JobExecutor 共享引擎锁安全；
但多进程部署（k8s 多 pod / 多 worker 进程）下，每个进程有独立内存 `_jobs`，
DB 才是真相源，必须用 DB 抢锁防止同一作业被多节点重复执行。

**范围决策（老板拍板）**：补「应该实现但没实现」的抢锁能力。优先级：先抢锁，后
REST 分页。

**Store 层 CAS 原语**（`camunda/persistence/store.py`）：

- **`acquire_due_jobs(lock_owner, lease_seconds, due_before, batch_size) -> List[Job]`**：
  三步式抢锁。1) SELECT 候选 ID（`duedate <= due_before AND retries > 0 AND
  (lock_owner IS NULL OR lock_expire_time < due_before)`，按 duedate 升序，limit
  = batch_size）。2) 逐条 `UPDATE ... WHERE id=:id AND (lock_owner IS NULL OR
  lock_expire_time < due_before) SET lock_owner=:me, lock_expire_time=:lease_until`
  —— affected_rows > 0 即抢到。3) 再 SELECT WHERE `lock_owner=:me AND lock_expire_time
  > :due_before` 取详情。**避开 SQL 方言差异**：SQLite / PostgreSQL 的 UPDATE LIMIT
  语法不同，「先 SELECT 后逐条 CAS」统一可移植。
- **`complete_job_cas(job_id, lock_owner) -> bool`**：CAS 删除（一次性作业用）。
  非 owner 调用返回 False，不删。
- **`reschedule_job_cas(job_id, lock_owner, new_due, new_retries, clear_lock=True)
  -> bool`**：CAS 更新 duedate + retries（timer-start 续排 / 失败重试顺延用）。
  `clear_lock=True` 默认：续排后清 LOCK 让下一轮任何 JobExecutor 可抢；
  `clear_lock=False`：保留锁（同步续约场景，调用方需自己管 lease）。
- **`extend_lock(job_id, lock_owner, lease_seconds, due_before) -> bool`**：CAS
  续约（长作业执行期间）；失败 = 锁已被别人接管，防御性中止提交。
- **`list_locks(lock_owner=None) -> List[Dict]`**：监控接口，看当前持锁情况。

**引擎侧 owner-aware 路径**（`camunda/engine/process_engine.py`）：

- 拆 `_execute_job` 为 `_run_job_body`（纯执行，失败向上抛）+ 原 `_execute_job`
  （执行 + `_persist_job_state` 落库，向后兼容）。
- 新增 `_execute_due_jobs_db(owner, lease_seconds, batch_size)`：调 `acquire_due_jobs`
  拿一批，对每个 job 从内存找上下文（实例上下文只能在内存里），跑 `_run_job_body`，
  完成后用 CAS 写回（`complete_job_cas` / `reschedule_job_cas(clear_lock=True)`）。
  失败路径：实例级 rollback 到上次同步点（DB LOCK 保留，内存重建后 lock_owner 是
  None）→ `_degrade_after_failure` → CAS reschedule（用函数参数 owner 而非
  mem_job.lock_owner，保证身份一致）→ 实例快照 save_proc_inst 全量落库。
- **LOCK 列竞态闭合**：`_persist_job_state` 全量重写 save_proc_inst 时 delete+insert
  会清掉 LOCK 列（无 lock_owner 字段）；但重写时 duedate 已推到未来（成功）或
  retry_delay 之后（失败），其他 JobExecutor 看到 `duedate > now` 不会立刻再抢。
  死信（retries=0）则因 acquire_due_jobs 的 `retries_ > 0` 过滤不会被重复抢。
- `execute_due_jobs` 加 `lock_owner` / `lease_seconds` 关键字参数：`lock_owner
  is not None and self._store is not None` 走 db 路径，否则走原内存路径。

**JobExecutor 改造**（`camunda/job/executor.py`）：

- `__init__` 新增 `lock_owner` 参数（默认 `_default_lock_owner(name)` 生成
  `name-pid-hostname-uuid8`，name 在最前便于人眼识别）；新增 `lease_seconds=300`。
- `tick()` 在 `_db_locking_enabled`（engine 有 Store）时改调
  `engine.execute_due_jobs(lock_owner=self._lock_owner, lease_seconds=self._lease_seconds)`。
- `shutdown` 不主动释放 lease（让其自然过期），便于模拟崩溃恢复测试。
- 暴露 `lock_owner` 属性 / `db_locking_enabled` 属性供运维 / REST 集成。

**验证**：

- 7 条 store CAS 单测 + 3 条集成测试（`tests/unit/test_job_locking.py`）全过：
  两个 JobExecutor 共享 store 验证同一 due job 只被其中一个执行；lease 过期
  后另一 owner 可重新抢；`extend_lock` / `complete_job_cas` / `reschedule_job_cas`
  非 owner 不操作。
- 全量回归 **241 passed**（M6 基线 231 + M7 新增 10，零回归）。
- `examples/run_lock_demo.py` 真实演示：双 owner 5 轮同步 tick，A/B 分别抢到
  部分作业（无重复执行）+ lease 过期接管。

**M7 文档化差异**（与 Camunda 7 Job Acquisition Row Lock 的差异）：

| 差异点 | Camunda 7 | camunda-python M7 |
|---|---|---|
| 抢锁原语 | SELECT FOR UPDATE 行锁（数据库行锁） | UPDATE CAS lease（应用层 CAS，跨方言） |
| lease 模式 | 加锁后自带 lock_exp_time，过期由 JobExecutor 扫描清理 | 同语义：过期由 `lock_expire_time < due_before` 命中自动失效 |
| 续约 | `JobExecutor.extendLockTimeout` | `store.extend_lock(job_id, owner, lease_seconds, due_before)` |
| 批量获取 | SELECT TOP N + 行锁 N 条 | SELECT 候选 ID + 逐条 CAS UPDATE（绕开方言差异） |
| 死信处理 | retries=0 仍可见但不被 acquire | 同：`acquire_due_jobs` WHERE `retries_ > 0` 过滤 |
| 单进程兼容性 | DB 路径是唯一路径（无 store 不工作） | store=None 时 JobExecutor 走内存路径（向后兼容旧用法） |
| 性能 | DB 行锁（N 条独立锁） | N 次小事务 CAS（每条独立事务，无锁等待） |

### M8 交付记录（2026-09-05）：REST 列表分页（firstResult + maxResults）

**问题（M6 留下的）**：9 个 GET 列表端点（process-instance / task / process-definition /
decision-definition / deployment / history 四类）始终全量返回 `list[dict]`；客户端要全量
拉回才能取下一页，对脚本/UI 都不友好。

**范围决策（老板拍板）**：实现 Camunda 7 REST 风格的 `firstResult` + `maxResults` 分页。
不做 count/total wrapper（响应仍是裸数组，调用方用「返回数 < maxResults」判定末页）——
保持响应形态不变，向后兼容。

**统一 helper**（`camunda/api/pagination.py`）：
- `DEFAULT_MAX_RESULTS = 200`：单页默认值（Camunda 文档中常见的「列表默认上限」）；
  M6 旧行为「无参即全量」会被这里默认 200 截断——所有受影响的列表端点都已迁移，
  业务侧若有需要可显式传 `?maxResults=1000`。
- `MAX_RESULTS_LIMIT = 1000`：单次硬上限。`?maxResults=99999` 会被 clamp 到 1000，
  防脚本误用拉空内存。
- `normalize_pagination(firstResult, maxResults)`：把入参 clamp 到合法区间，
  返回 `{firstResult, maxResults}`。
- `paginate(items, firstResult, maxResults)`：纯函数切片，越界返回 `[]`。

**HTTP 层**（9 个路由同步改造）：
- 每个列表端点加 `firstResult: int = Query(ge=0, default=0)` + `maxResults:
  int = Query(ge=1, default=DEFAULT_MAX_RESULTS)` 两个查询参数。
- 端点内部：先按业务过滤（processDefinitionKey / businessKey / finished 等），
  再 `paginate(items, firstResult, maxResults)` 返回。
- 非法值（负 firstResult / 0 或负 maxResults）由 FastAPI `Query(ge=...)` 在路由前
  拦截返回 422，不进 helper。

**验证**：
- 27 条单测（`tests/unit/test_api_pagination.py`）：12 条 helper 纯函数 +
  15 条 HTTP 端点（含 9 个端点冒烟 + 分页与过滤复合 + 越界 + 超限 clamp + 非法值 422）。
- 全量回归 **268 passed**（M7 基线 241 + M8 新增 27，零回归）。
- `examples/run_api_demo.py` 追加 `demo_pagination`：起 5 个实例 + 翻 3 页 +
  越界 + 超限 clamp + 非法值 422 + history 分页演示，全部通过。

**M8 文档化差异**（与 Camunda 7 REST 分页约定的差异）：
| 差异点 | Camunda 7 | camunda-python M8 |
|---|---|---|
| 响应形态 | 裸数组 | 同（不包 count/total；末页判定靠 `len(resp) < maxResults`） |
| maxResults 默认 | 不设硬上限（视端点而定） | 200 / 硬上限 1000（防止脚本误用） |
| 非法 firstResult | 400 + 错误体 | 422（FastAPI `Query(ge=0)` 默认行为） |
| 越界 firstResult | 空数组 | 同（`paginate` 越界返回 `[]`） |
| 过滤 + 分页复合 | 端点支持多过滤参数 + 分页 | 同（先过滤后分页，单条 SQL 视角等效） |

### M6 交付记录（2026-09-05）：REST API（FastAPI 兼容层）

**范围决策（老板拍板）**：M5 DMN 与 M6 REST 都做（先 M5 后 M6）。M6 覆盖 Camunda
engine-rest 的常用端点子集；**不做** Cockpit / Tasklist Web 控制台（见第 1 章「不做」范围）。

**切片说明**：M6-1 脚手架（依赖 + app 工厂 + 异常映射 + DTO）；M6-2 部署与流程定义；
M6-3 流程实例；M6-4 任务；M6-5 历史；M6-6 DMN 决策；M6-7 测试 + demo + 文档。

**分层结构**（`camunda/api/`）：

| 模块 | 职责 |
|---|---|
| `app.py` | `create_app(engine=None, prefix="/engine-rest")` 工厂；路由装配；`/`、`/health` |
| `errors.py` | CamundaException 层次 -> HTTP 状态码；统一错误体 `{"type","message"}` |
| `schemas.py` | 请求/响应 DTO + 变量序列化（包装形态 / 裸值双向兼容） |
| `deps.py` | 引擎注入（`request.app.state.engine`），规避 routers <-> app 循环 import |
| `routers/` | 六类资源路由：deployment / process-definition / process-instance / task / history / decision-definition |

**引擎增量（为支撑 REST 补的门面能力，均为小增量、零回归）**：
`list_process_definitions` / `list_deployments` / `get_process_definition_xml`（新增
`_definition_sources` 保存部署时原始 XML）/ `delete_process_instance`（新增
`Store.delete_proc_inst`：清 RU 行 + HI_PROCINST 置 DELETED）/ `get_task` /
`claim_task` / `unclaim_task` / `set_assignee` / `set_variable`；DMN 侧新增
`DmnEngine.list_decisions`。任务级变更（认领/指派）后同步实例快照，保证 assignee 落库。

**关键设计**：
- **变量双形态**：入参兼容 Camunda 包装形态 `{"amount": {"value": 20000, "type": "Long"}}`
  与裸值 `{"amount": 20000}`（引擎按 Python 原生类型处理，type 仅文档化）；出参默认包装
  形态，带 `?bare=true` 退化为裸值 map 便于脚本直用。
- **异常映射**：NotFoundException 404 / DeploymentException、InvalidRequestException 400 /
  ProcessInstanceException 409（实例状态冲突）/ ExpressionEvaluationException 400 /
  CamundaException 兜底 500。响应体对齐 Camunda 的 `{"type", "message"}`。
- **部署分派**：BPMN 与 DMN 的 XML 根元素都叫 `definitions`，按**根元素的子元素**判定
  （含 `decision` -> DMN，含 `process` -> BPMN），multipart 一次可混合部署两类。
- **删除实例语义**：对齐 Camunda 默认（不传 skipHistory）——清运行时态与 RU 行，
  `ACT_HI_PROCINST` 置 `DELETED` 并写 end_time，ACTINST/TASKINST/VARINST 历史保留。
- **历史数据源**：统一走内存视图（实例完成后仍留在 `_instances`，仅 state 置 COMPLETED），
  保证有无 Store 两种模式行为一致。

**踩坑 1（重要，易复发）**：FastAPI 配合 `from __future__ import annotations` 时，路由函数
签名里的注解是**字符串**，需能在模块 globals 中解析。`decision.py` 漏 import `Optional`
导致 `Optional[EvaluateDecisionDto]` 解析失败——**FastAPI 不报错，而是静默丢掉该 body
参数**（请求体收不到，参数恒为 None，表现为「变量为空」这类诡异现象）。同类风险：
`body: Any = None` 也会被当成非 body 参数，必须显式写 `Body(default=None)`。
排查手段：对路由函数跑 `typing.get_type_hints(fn)`，抛 NameError 即已中招（本次已加
全量校验，6 个 router 模块 0 失败）。

**验证**：20 条 REST 单测（端点覆盖 + 异常映射 + 变量双形态）；全量回归 **231 passed**
（M5 基线 211，零回归）。`examples/run_api_demo.py` 真实起 uvicorn + 真实 HTTP 端到端
演示通过（部署 / 启动 / 认领完成 / 四类历史 / 决策求值 / 异常映射）。

**M6 文档化差异**（与 Camunda 7 REST 差异）：
| 差异点 | Camunda 7 | camunda-python M6 |
|---|---|---|
| 端点覆盖 | engine-rest 全量（含 identity/authorization/filter/外部任务等） | 六类常用子集：deployment / process-definition / process-instance / task / history / decision-definition |
| 启动方式 | definitionKey / definitionId / message 启动 | 仅 definitionKey（缺则 400；definitionId / message 启动不支持） |
| 部署通道 | multipart（`data` 字段） | 同；额外提供 JSON 便捷通道 `POST /deployment/create/xml`（本项目扩展） |
| 变量入参 | 包装形态 VariableValueDto | 包装形态与裸值均收 |
| 变量出参 | 包装形态 | 同；`?bare=true` 可退化为裸值 map（本项目扩展） |
| 决策求值结果 | DmnDecisionResultEntries 包装 | 引擎原始结果（标量 / dict / 列表 / None），不额外包装 |
| 任务变量 | 任务级变量（TaskService 局部作用域） | 返回所属实例的变量全集（本项目变量实例级，无作用域隔离） |
| 删除实例历史 | `skipHistory` 可选是否保留 | 固定保留（HI_PROCINST 置 DELETED），不支持 skipHistory |
| 历史查询 | 查 ACT_HI_* 表 | 统一走内存视图（含已结束实例）；被 DELETE 删除的实例在内存视图中不再可见 |
| 分页 / 排序 / 高级过滤 | 支持（firstResult/maxResults、orQueries 等） | 不支持（仅常用等值过滤，全量返回） |
| 认证鉴权 | 支持（basic auth / JWT） | 不支持（引擎无 identity 模块） |

### M5 交付记录（2026-09-05）：DMN 决策引擎 + FEEL 子集

**范围决策（老板拍板）**：纯决策表（decisionTable）形态。literalExpression / relation /
invocation / context 等非决策表形态**部署期明确报错**（文档化差异，对齐 M4-2c 宿主白名单做法）。

**切片说明**：M5-1 = 模型 + 解析器；M5-2 = FEEL 子集求值器；M5-3 = 决策表引擎（hitPolicy
收敛）；M5-4 = businessRuleTask 引擎集成；M5-5 = demo 自验证；M5-6 = 文档与日志。

**模型与解析（M5-1）**：`DmnModel` 作为部署单元（一份 *.dmn 可含多个 Decision，对齐
BpmnModel 的部署单元角色）；`Decision` 承载 decisionTable；`DmnInput.expression` =
inputExpression 文本（FEEL 表达式，如 `amount`）；`DmnRule` 的 input/output entries 与列按
**下标一一对应**，`_normalize_entry` 把空文本 / `"-"` 归一为 None（输入侧 = 通配恒命中，
输出侧 = 空输出，服务 COLLECT COUNT 语义）。

命名空间策略：不校验版本（DMN 1.1 / 1.3 同构），只用 localName 分派。解析期校验：hitPolicy
白名单（UNIQUE/FIRST/ANY/PRIORITY/RULE ORDER/COLLECT）；aggregator 仅 SUM/MIN/MAX/COUNT 且
**仅在 hitPolicy=COLLECT 下合法**（否则部署报错）；rule 的 inputEntry/outputEntry 数量与列
数严格对齐（DMN 规范要求）；至少 1 个 output 列；`outputValues` 按逗号切分且**尊重引号内
的逗号**。

**FEEL 求值器（M5-2）**：手写递归下降，无外部依赖；parse 即 eval，**无 AST 中间层**（决策表
单元格表达式足够小，省一层反而更直白）。

- **unaryTests**（输入单元格，配合输入值求布尔）：通配（None）恒命中；比较算子
  `= != < <= > >=`；区间 `[a..b]`（闭闭）/ `(a..b)`（开开）/ `]a..b[` 与 `(a..b]` 混合开闭
  （DMN 双标记法均支持）；逗号列表 = OR；`not(...)` 取反；`null` 字面量判缺变量。
- **expression**（输出单元格 / inputExpression）：字面量（number / "string" / true / false /
  null）；变量引用（**未定义 -> null**，对齐 FEEL 缺变量语义）；算术 `+ - * /` 与一元负号、
  括号，标准优先级；字符串 `+` 拼接。
- **语义细节（易踩）**：null 参与排序比较（`< <= > >=`）与区间判定 = **false（规则不命中）**，
  不是报错；数值相等跨 int/float（1 == 1.0）；bool 不支持排序比较；除零 / 字符串参与算术 /
  函数调用一律明确报错。词法层 `..` 优先于小数点消费（否则 `[1..5]` 会被切成 1. 和 .5）。

**决策表引擎（M5-3）**：`DmnEngine` 独立可脱离 BPMN 单用，deploy 返回 decision key 列表
（重复 key 版本 +1，对齐 ACT_RE_DECDEF 多版本语义）。求值三步：输入列求值 -> 规则命中过滤
（全列 unaryTests 相与）-> hitPolicy 收敛。

- **结果形态**：单输出列 -> 标量；多输出列 -> `dict{output键: 值}`（键取 `name` 优先，回退
  `label` / `id`，对齐 Camunda DmnDecisionResult）。
- **UNIQUE**：多行命中 -> 运行时 ExpressionEvaluationException（DMN 规范违例）。
- **FIRST** 取命中序首行；**ANY** 各行输出不一致 -> 运行时报错。
- **PRIORITY**：按 output 的 `outputValues` 声明序取最高优先级（下标越小越高，未声明取值排
  最低）；仅支持单输出列，多列明确报错。
- **RULE ORDER** / **COLLECT**（无聚合）：行结果列表（按命中顺序）。
- **COLLECT + SUM/MIN/MAX**：标量（要求恰好 1 个输出列且输出为数值）；**COLLECT + COUNT**：
  命中行数（int）。
- **无命中不抛异常**：UNIQUE/FIRST/ANY -> None；RULE ORDER/COLLECT -> `[]`；COLLECT COUNT -> 0
  （对齐 Camunda 空结果语义）。

**businessRuleTask 集成（M5-4）**：ProcessEngine 新增 DecisionService 语义门面
（`deploy_dmn` / `evaluate_decision` / `get_decision_definition` / `get_decision_version`）。
节点行为 = **同步求值 + 结果写入 result_variable**，无等待窗口（与 serviceTask 同形态，
`_open_activity` -> 求值 -> `_close_activity` -> `_leave`），支持 asyncAfter 拆分。决策结果
写入实例变量后可直接驱动排他网关选路（demo 场景 3：C 级进人工复核，其余自动通过）。

**验证**：53 条 DMN 单测（解析 10 + FEEL/引擎 37 + businessRuleTask 集成 6）全绿；
全量回归 **211 passed**（M4-2d 基线 158，零回归）。`examples/run_dmn_demo.py` 三场景演示通过
（直接求值 UNIQUE 单行命中 / 决策联动 COLLECT+SUM 消费上一决策输出 / businessRuleTask 驱动
网关选路）。

**已知限制**：DMN 部署**不落库**（对齐 delegate 注册不落库的既有先例）——崩溃恢复后须重新
`deploy_dmn`，否则 businessRuleTask 求值报未部署。若后续需要决策定义持久化，可照
ACT_RE_DECDEF 补表（当前无此需求，成本收益不划算）。

**M5 文档化差异**（与 Camunda 7 DMN 引擎差异）：
| 差异点 | Camunda 7 | camunda-python M5 |
|---|---|---|
| 决策形态 | decisionTable / literalExpression / relation / invocation / context | 仅 decisionTable，其余部署期明确报错 |
| FEEL 支持 | FEEL 全量（含函数/日期时间/路径/内置函数库） | Friendly 子集：比较/区间/OR/not/null + 算术/字符串拼接；函数调用、between、in、日期时间、路径表达式均明确报错 |
| 未定义变量 | null（不报错） | 同（对齐 FEEL 缺变量语义） |
| PRIORITY 多输出列 | 支持 | 明确报错（仅单输出列按 outputValues 取最高） |
| COLLECT 聚合 | SUM/MIN/MAX/COUNT | 同 |
| 无命中 | 空结果（不报错） | 同（None / `[]` / COUNT=0） |
| 决策定义持久化 | ACT_RE_DECDEF / ACT_RE_DECISION_DEF 落库 | 不落库，崩溃恢复须重新 deploy_dmn |
| 命名空间 | 校验 DMN 1.1/1.3 | 不校验（按 localName 解析，1.1/1.3 同构） |

### M4-2a 交付记录（2026-09-02）：内嵌子流程（embedded SubProcess）+ 边界 timer 中断 scope

**切片说明**：M4-2 = 子流程家族。M4-2a = 内嵌子流程（embedded subProcess）—— 事件子流程、
多实例、非中断式边界的地基。三层交付：解析器容器递归（M4-2a1）-> 引擎展开/收束（M4-2a2）
-> 边界 timer 中断整段 scope（M4-2a3）-> 持久化验证（M4-2a4）。

**模型与解析（容器递归）**：
- SubProcess 是父容器内的活动节点、自身又是独立容器：`process` 字段持有内部 Process
  （flow_nodes/sequence_flows/start_events 与顶层同构）。解析按 4 遍结构递归
  （sequenceFlow -> flowNode/subProcess 递归 -> 连线挂接 -> 边界归属），每层独立校验。
- 跨容器引用部署即报错：内层连线 targetRef 指向外层节点 -> `不存在于 process 'sub::inner'`。
- 普通内嵌子流程必须至少一个内部 startEvent（校验递归进内层）；triggeredByEvent=true
  （事件子流程）解析宽容、运行时明确报错（M4-2b）。

**引擎执行（容器感知流转）**：
- `_container_of(pi, e)`：沿 execution 父链找最近「停驻在 SubProcess 上的 SCOPE 祖先」，
  取其 inner Process 即当前容器；无则根 Process。所有流转/作业归属按容器解析 —— 跨容器
  同名节点 id 不串扰（complete_task、边界 job 触发、join 还原全部容器化）。
- 进入 subProcess（`_enter_subprocess`）：token 转 SCOPE 停驻（activity=subProcess id），
  subProcess actinst 跨整段内部执行 open（对齐 Camunda HI_ACTINST 覆盖区间）；spawn 内部
  子 token 从内部 startEvent 推进。变量作用域沿用实例级（文档化差异）。
- 收束复活（`_collapse_scopes` 泛化，M1 只收 root）：自底向上扫描「无活跃子的 SCOPE」——
  停在 ParallelGateway（并行分支各自直通 end、无 join）-> 结束自身逐层向上收；停在
  SubProcess（内部全部走完）-> 结算 actinst、恢复 TOKEN 沿 sub 出边继续。
- **防误收**：并行 join 汇聚后恢复的 SCOPE 立即复位 role=TOKEN（停在普通等待节点时不被
  收束扫描误杀）；collapse 只收 SubProcess / ParallelGateway 停驻的叶子 SCOPE。
- 嵌套子流程递归成立（sub2 收束 -> sub1 收束 -> root）；外层并行分支含子流程 + 另一分支
  直通 end 的组合不误收内部主线。

**边界 timer 中断子流程（M4-2a3）**：
- subProcess 是合法边界宿主：进入时注册 timer-boundary job（等待窗口 = 整段内部执行期），
  正常收束离开时撤销。
- 到期触发 = **整段 scope 取消**（`_kill_subprocess_scope`）：自底向上结束内部全部活跃
  execution（结算 open actinst、归档待办任务留 HI_TASKINST、删除所属实例级作业、从
  join_arrivals 摘除登记、detach 摘树），再结算 subProcess actinst，token 沿边界事件出边走。
- 同步 serviceTask / 无等待窗口的活动照旧不触发（宿主规则不变）；非中断式
  cancelActivity=false 当时明确报错，M4-2b4 起放开普通等待活动宿主（见 M4-2b 记录）。

**持久化与验证**：execution 树（role/activity/parent）与 job 快照既有列透传，容器归属由
树形静态推导（重启重解析后同样成立），**无需表结构改动**。76 个单测全绿（6 解析 + 12
引擎 + 3 持久化新增）；examples/run_subprocess_demo.py 真实时钟 + JobExecutor 演示通过
（正常履约 / 超时退款两路，约 5 秒）。

**M4-2a 文档化差异**（与 Camunda 7 差异）：
| 差异点 | Camunda 7 | camunda-python M4-2a |
|---|---|---|
| 变量作用域 | 子流程内可建子作用域变量 | 实例级单一作用域（无局部遮蔽） |
| 并行约束 | 任意嵌套并行 | 任何容器内并行分支路径不嵌套并行网关（沿用 M1 约束） |
| subProcess asyncAfter | 支持（离开拆作业） | 明确报错；asyncBefore 支持（= 展开前异步窗口） |
| 事件子流程 triggeredByEvent | 支持 | 解析保留、运行时明确报错（M4-2b） |
| 子流程内 timer startEvent | 支持 | 明确报错（内嵌子流程不启动定时实例） |
| 非中断式边界 cancelActivity=false | 支持并发分支 | M4-2a 拒绝；**M4-2b4 起支持普通等待活动宿主**（subProcess 宿主仍拒绝，见 M4-2b 差异表） |

### M4-1 交付记录（2026-09-02）：timer 边界事件 + asyncAfter

**切片说明**：M4（BPMN 全元素补齐）按增量切片推进。M4-1 = 中断式 timer 边界事件 +
asyncAfter 行为后拆分 —— 与 M3 作业语义同构，新增两种实例级 job_type：
`timer-boundary`（宿主等待期内到点触发）与 `async-after`（行为完成后异步离开）。

**中断式 timer 边界事件（BoundaryEvent，仅 timer 变体）**：
- 解析：boundaryEvent 不入主流转（无 incoming 参与 join/fork 计数），attachedToRef
  归属回填 `flow_nodes[host].boundary_events`；校验宿主存在且非 start/end/boundary。
- 宿主 = **有等待点的活动**：userTask（停等 complete）与 asyncBefore 节点（行为拆分
  停等）。到达宿主停等时注册 timer-boundary job（duration: now+delay / date: 绝对
  时点）。同步 serviceTask 无等待窗口 -> 不注册不触发（对齐 Camunda：同步活动在单
  命令内完成，边界无法插入中断）。
- 到期触发（`_fire_timer_boundary`）：先防御校验 token 仍停在宿主且 actinst 未结算
  （宿主已离开 = 过期作业丢弃）-> 取消宿主（① userTask 归档 completed_tasks 并结算
  end_time，HI_TASKINST 留痕 ② 结算宿主 actinst ③ 撤销宿主未执行的
  async-continuation 行为作业 ④ 删除宿主全部 timer-boundary job）-> token 改走边界
  事件出边推进（无出边即收束）。
- 宿主正常离开（complete_task / async 行为完成离开 / join 汇聚）-> 同步撤销边界 job；
  asyncBefore+userTask 组合行为（建任务）后仍停等宿主 -> 边界继续有效可中断；
  asyncBefore 行为失败降级重试窗口内边界仍可中断 -> 未执行的重试作废。
- 边界事件自身 open+close 留 actinst 痕迹（HI_ACTINST 可回溯中断路径与时间）。

**asyncAfter（行为后拆分「离开推进」）**：
- 类型范围：serviceTask / exclusiveGateway；其余类型（userTask / 并行网关 / 事件类
  等）到达时明确报错（文档化差异，避免静默错位）。
- serviceTask：delegate 行为在到达命令内同步执行（或 asyncBefore 行为 job 内），
  actinst 结算后注册 async-after job（duedate=now），到期执行离开（多出边 fork）。
- exclusiveGateway：网关无副作用，选路推迟到 job 到期 —— 离开时按到期时刻变量
  重新求值出边条件（行为与离开之间的异步窗口内变量可变化）。
- asyncBefore + asyncAfter 链式：行为 job 完成 -> 再拆 async-after job，两段推进。
- 过期防御：async-after 到期时 token 已离开节点（activity_id != job.node_id）-> 丢弃。

**持久化与验证**：新 job_type 经实例 RU 快照透传（JobEntity.job_type 列既有），
from_database 原样还原，中断取消的任务归档/actinst 结算复用 M2 HI 全量重写路径，
**无需表结构改动**。55 个单测全绿（含 14 边界语义 + 3 持久化恢复）；
examples/run_boundary_demo.py 真实时钟 + JobExecutor 演示通过（正常审批/超时降级两路）。

**M4-1 文档化差异**（与 Camunda 7 差异）：
| 差异点 | Camunda 7 | camunda-python M4-1 |
|---|---|---|
| 非中断式边界 cancelActivity=false | 支持并发分支 | 解析保留、运行时明确报错（随子流程 M4-2 落地） |
| 边界 timerCycle | 允许（周期中断） | 拒绝（同 timer-catch：cycle 仅用于 timer start） |
| 边界宿主范围 | 任意活动（含子流程/事件） | 仅 userTask / asyncBefore 节点；同步 serviceTask 的边界不触发 |
| asyncAfter 类型范围 | 通用（任意 activity） | 仅 serviceTask / exclusiveGateway，其余明确报错 |
| 边界与 asyncAfter 组合 | 活动结束后边界仍可能有收尾语义 | actinst 结算即撤销边界（asyncBefore 宿主行为完成即结算） |

### M3 交付记录（2026-09-02）

**作业模型（对齐 ACT_RU_JOB 语义）**：三种 job_type —— `timer-catch`（token 停在 timer 中间捕获
事件，实例级）、`timer-start`（定义级，无实例，触发即启动流程）、`async-continuation`
（camunda:asyncBefore 把节点行为执行拆成独立作业，实例级）。

**timerEventDefinition 三分派**：
| 子元素 | 语义 | startEvent | intermediateCatchEvent |
|---|---|---|---|
| `timeDuration` | ISO-8601 相对延迟（解析期算好秒数，非法部署即报错） | ✅ 一次性定时启动 | ✅ 停等到期 |
| `timeDate` | 绝对时间点（Z/偏移归一化本地时区） | ✅ | ✅ |
| `timeCycle` | ISO `R[n]/PT..` 间隔重复 或 quartz/cron 表达式 | ✅ 触发后续排 | ❌ M3 拒绝（Camunda 允许，文档化差异） |

**执行链**：
- token 到 timer catch → open actinst + 注册 timer-catch job（duedate）→ 停等；到期 `_fire_timer_catch`
  结算 actinst 沿出边推进。
- asyncBefore 节点 → `_open_activity`（actinst 保持 open）+ 注册 duedate=now 的 async job → 停等；
  job 执行时 `_dispatch_node` 直接跑行为，open actinst 复用（async job 内不会再拆 async，防死循环）。
- asyncAfter M3 解析保留、运行时明确报错（M4-1 已实现 serviceTask/XOR 行为后拆分，见下节）。
- `execute_due_jobs()` 快照到期非死信作业逐个执行（排序按 duedate）；**单条失败不打断整轮轮询**
  —— retries-1（默认 3，`DEFAULT_MAX_RETRIES`）、未耗尽按 retry_delay_seconds 顺延 duedate、
  耗尽即死信保留记录不再 acquire（对齐 Camunda JobEntity.retries 语义）。
- timer-start cycle 续排：interval 按「计划 duedate 链式 + 周期」续排（执行延迟不累积漂移，
  错过不补触发）；count 递减到 0 停排（R3/PT10S = 触发 3 次）；cron 从当前时刻求下一触发（无限）。

**JobExecutor（camunda/job/executor.py）**：后台线程周期性调 `execute_due_jobs()`；`tick()` 单步
方法供手动/测试；Event.wait 空闲等待、shutdown 即时退出。**引擎级 RLock**：所有命令入口与
JobExecutor 轮询共用，单进程内用户命令与作业执行天然互斥。

**多 JobExecutor 抢锁（M7）**：多进程部署下 JobExecutor 自动分配唯一 `lock_owner`
（`name-pid-hostname-uuid8`），engine 持有 Store 时走 `_execute_due_jobs_db` 路径：
store.acquire_due_jobs 用 `UPDATE ACT_RU_JOB SET LOCK_OWNER=me, LOCK_EXP_TIME=now+lease
WHERE id=:id AND (LOCK_OWNER_ IS NULL OR LOCK_EXP_TIME_ < :now)` CAS 抢锁（避开 SQL
方言差异：先 SELECT 候选 ID 再逐条 CAS）。完成后用 `complete_job_cas` / `reschedule_job_cas`
写回（防御性 owner 校验）。`extend_lock` 续约（长作业）。`list_locks` 监控。详见 M7 交付记录。

**可注入时钟（common/clock.py）**：引擎/执行器统一取 `clock.now()`（定长 ISO，字典序可比）；
测试 `set_clock` 拨时间即到期，无需真等待。时长/周期解析在 common/timers.py（cron 用 croniter）。

**M3 持久化与崩溃恢复**：实例级作业随实例 RU 全量重写（同 M2 事务边界）；定义级 timer-start
作业独立全量组重写（`PROC_INST_ID_ IS NULL`，store.save_timer_start_jobs）。`from_database()`
恢复定义级作业挂回引擎、实例级作业随实例快照重建（含 repeat/retries 还原）。**持久化实例级
作业执行失败自动回滚内存到上次同步点**（从 RU/HI 重读重建）再降级重试，避免「内存已推进、
库未写」的半执行不一致。

**M3 文档化差异**（与 Camunda 7 差异）：
| 差异点 | Camunda 7 | camunda-python M3 |
|---|---|---|
| timerCycle on catch | 允许 | 拒绝（`_handle_timer_catch` 抛 InvalidRequestException） |
| 失败重试间隔 | failedJobRetryTimeCycle 表达式 | 固定 `retry_delay_seconds`（默认 5s），未解析该扩展 |
| 作业锁 | LOCK_OWNER_/LOCK_EXP_TIME_ 多实例抢占 | 字段预留但单进程 RLock 已足；多进程抢锁 M4+ |
| interval 补触发 | JobExecutor 周期轮询（可补） | 不补触发，按计划链续排不漂移 |
| 定时流程手动启动 | 报错（引擎语义一致） | start_process_instance_by_key 抛 ProcessInstanceException |

### M2 交付记录（2026-09-02）

**事务边界同步策略**：M1 引擎在内存推进，M2 在**每个命令边界**（deploy / start_process_instance /
complete_task）把实例状态全量同步到库。崩溃发生在命令中途 => 该命令整体丢失（等价 Camunda 单命令事务）。
- RU 表：delete+insert 该实例当前 ACTIVE 状态（Camunda 逐行 update；数据量小，全量重写简单且一致）
- HI 表：PROCINST upsert 一行；ACTINST/TASKINST/VARINST 按实例全量重写

**M2 文档化差异**（与 Camunda 7 的表契约差异）：
| 差异点 | Camunda 7 | camunda-python M2 |
|---|---|---|
| BPMN 资源 | ACT_GE_BYTEARRAY 存二进制 | 直接存 `ACT_RE_PROCDEF.RESOURCE_XML_` 文本，恢复时重解析 |
| 变量作用域 | 变量挂 execution（可局部） | 实例级快照，RU_VARIABLE/HI_VARINST 挂 proc_inst（复合主键） |
| HI_VARINST 版本 | 每次变更追加版本行 | 每实例每变量一行（当前值快照） |
| 引擎门面 API | REST / Java Service | `deploy()` / `start_process_instance_by_key()` / `complete_task()`；`ProcessEngine(store=...)` 或 `from_database(url)` |
| 裸路径 URL | - | Store 自动把 `/abs/path.db` 归一化为 `sqlite:////abs/path.db` |

**恢复语义**：`from_database()` 从 ACT_RE_PROCDEF 每 key 取最新版本重解析 XML；从 ACT_RU_* 重建
execution 树、变量、待办任务；停等在并行 join 网关的 ACTIVE TOKEN 重建 join_arrivals 等待登记；
未结算的活动实例（end_time 为空，如停在 userTask）挂回对应 execution 以便后续 close 结算。
delegate 注册不落库（对齐 Spring bean 语义），恢复后需自行 `register_delegate` 同名实现。

## 5. 技术选型（2026-09-02 老板拍板 ✅）

| 决策点 | 结论 | 说明 |
|---|---|---|
| 对齐目标 | **语义对齐为主** | 行为/状态机/持久化契约对齐，REST API 兼容层 M6 后置 |
| 持久化 | **SQLAlchemy 2.0** | ORM + SQLite(dev)/PostgreSQL(prod) 双方言；M2 引入，先留接口 |
| BPMN 解析 | **lxml 自研解析器** | 构建 BpmnModelInstance 等价物，完全掌控模型层 |
| 表达式 | 自研安全子集 | M1 简化：支持 `${x > 3}` 类条件，M4 完善 |
| ID 生成 | UUID | 对齐 ACT 表主键语义 |
| 打包 | pyproject.toml | 见工程根目录 pyproject.toml |
| 运行时 | Python 3.12+ | 类型标注 + dataclass |

**M1 范围明确约束**（增量开发原则）：
- 支持元素：startEvent / endEvent / userTask / serviceTask / exclusiveGateway / parallelGateway(fork+单层 join) / sequenceFlow(条件)
- 并行分支内暂不支持嵌套并行网关与循环（join 语义简化），M4 全元素补齐时强化
- serviceTask 通过 delegate 注册表对接 Python 可调用对象
- M1 为内存版：进程重启后实例不可恢复（M2 持久化解决），运行痕迹暂不写 ACT_HI 表

## 6. 风险与取舍

1. **Command 拦截器栈**（事务/权限分层）是 Camunda 精髓——Python 侧若用 SQLAlchemy session 天然事务，可简化，但需保留 `@command` 装饰器语义便于后续加锁重试。
2. **execution 树**：Camunda 的父-子 Execution 是并行网关/子流程正确性的关键，M1 必须按树建模，不能拍平成"当前节点"。
3. **Job 轮询**：多进程部署时抢锁靠 `FOR UPDATE SKIP LOCKED`（PG）/ 文件锁（SQLite 单进程演示）。
4. **Type 映射**：`ObjectValue`/序列化变量（Java 序列化无对应物）——用 JSON/pickle 策略替代并文档化差异。
