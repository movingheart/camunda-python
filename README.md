# camunda-python

用 Python 3 语义对齐重写 Camunda 7 BPMN 引擎（Apache-2.0 独立实现，不搬运 Java 源码）。

**第一次来？** 先看 [docs/USER_GUIDE.md](docs/USER_GUIDE.md)（从「5 分钟跑通 hello world」到「生产部署」）。
**想知道项目覆盖了哪些特性 / 跑测试 / 怎么用每个 API？** 看本文档。
**想了解设计决策 / 与 Camunda 7 的差异？** 看 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 状态

- **M0~M8 已交付**（M4 全家族 + M5 DMN + M6 REST + M7 多 JobExecutor 抢锁 + M8 REST 列表分页）：工程骨架 + BPMN 模型层 + lxml 解析器 + 内存版引擎核心流转 +
  SQLAlchemy 2.0 持久化（ACT_RE/RU/HI 三套表）+ 崩溃恢复 + 历史写入 +
  作业执行器（Timer Start / Timer Catch / asyncBefore / asyncAfter / 失败重试 / 轮询）+
  timer 边界事件（中断式，宿主 userTask / asyncBefore 节点）+ **内嵌子流程**
  （embedded SubProcess：容器递归解析 / 展开与收束复活 / 边界 timer 中断整段 scope）+
  **事件子流程**（error end 冒泡捕获 / timer start 订阅触发）+ **非中断式边界事件**
  （cancelActivity=false：宿主不取消、spawn 并发线；root 停驻等待并发子树收束）+
  **多实例**（MultiInstanceLoopCharacteristics：userTask / serviceTask / subProcess 三宿主 +
  completionCondition 提前终止 + loopCounter/元素变量 + 持久化恢复）+ **消息/信号事件**
  （correlate_message 1:1 关联 + throw_signal 跨实例广播 + catch/边界/事件子流程 start/中间
  与结束抛出全形态 + 非中断订阅常驻 + 恢复重推导）+ **DMN 决策引擎**
  （决策表解析 + FEEL 子集求值 + 六种 hitPolicy 收敛 + businessRuleTask 集成）+ **REST 兼容层**
  （FastAPI 对齐 Camunda engine-rest：部署 / 流程定义 / 流程实例 / 任务 / 历史 / 决策六类端点 +
  9 个列表端点 firstResult + maxResults 分页）+ **多 JobExecutor 抢锁**
  （DB CAS lease：同一作业不会被多节点重复执行 + lease 过期可被接管 + 续约）
- 参考基线：Camunda 7.23.0（社区版最后开源稳定版）
- 架构与里程碑见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## 快速开始

```bash
cd /Users/yingwang/CodeSpace/camunda
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"        # 跑测试
pip install -e ".[api]"        # 用 REST（M6，含 fastapi/uvicorn）

# 跑贷款审批示例（排他网关 + 用户任务 + 服务任务）
python examples/run_demo.py

# 跑定时器/作业示例（真实时钟 ~15 秒：自动定时启动 + async 拆分 + 冷却等待 + 崩溃恢复）
python examples/run_timer_demo.py

# 跑边界事件/asyncAfter 示例（真实时钟 ~6 秒：审批超时自动降级 + asyncAfter 审计日志）
python examples/run_boundary_demo.py

# 跑内嵌子流程示例（真实时钟 ~5 秒：订单履约子流程 + 边界超时整段中断退款）
python examples/run_subprocess_demo.py

# 跑非中断式边界示例（真实时钟 ~6 秒：工单催办宿主保留 + 并发复核收束）
python examples/run_ni_demo.py

# 跑多实例三宿主示例（纯同步驱动：serviceTask 同步推送 / subProcess 顺序逐店 /
#                       subProcess 并行 + completionCondition 抢跑终止）
python examples/run_mi_demo.py

# 跑消息/信号事件示例（纯同步驱动：跨实例消息接力 / 信号广播通知 / 崩溃恢复）
python examples/run_msg_sig_demo.py

# 跑 DMN 决策示例（纯同步驱动：直接求值 / 决策联动 COLLECT+SUM / businessRuleTask 集成）
python examples/run_dmn_demo.py

# 跑 REST API 示例（真实起 uvicorn + 真实 HTTP：部署/启动/任务/历史/决策/异常）
python examples/run_api_demo.py

# 跑多 JobExecutor 抢锁示例（同步驱动 + 真实 sleep：双 owner 共享 store，
#        同一 due job 只被其中一个执行 + lease 过期可被另一 owner 接管）
python examples/run_lock_demo.py

# 跑测试（268 个单测：M1 流转 + M2 持久化 + M3 作业 + M4-1 边界/asyncAfter +
#        M4-2a 子流程 + M4-2b 事件子流程/非中断式边界 + M4-2c 多实例 +
#        M4-2d 消息/信号事件 + 持久化恢复 + M5 DMN 解析/FEEL/引擎/businessRuleTask +
#        M6 REST 端点与异常映射 + M7 store CAS lease 抢锁原语 + 多 executor 防双执行 +
#        M8 REST 列表分页（9 个端点 + clamp/边界））
pytest
```

## 目录结构

```
camunda/
├── model/        # 纯数据模型：BpmnModel / Process / FlowNode / Execution / Task / Variable
├── parser/       # lxml 自研 BPMN 2.0 解析器
├── engine/       # 引擎门面 + 节点行为（M1 内存版）
├── job/          # 作业执行器（M3）
├── persistence/  # SQLAlchemy 持久层（M2）
├── api/          # REST 兼容层（M6）
├── dmn/          # DMN 决策引擎（M5）
└── common/       # 异常层次、ID 生成器
```

## 引擎用法

**内存版（M1）**——无 DB 依赖：

```python
from camunda.parser import parse_bpmn_xml
from camunda.engine import ProcessEngine

engine = ProcessEngine()
with open("examples/loan-approval.bpmn") as f:
    model = parse_bpmn_xml(f.read(), source_name="loan-approval.bpmn")
engine.deploy(model)                                   # 部署 -> ["loan-approval"]

pi = engine.start_process_instance_by_key(
    "loan-approval", {"applicant": "张三", "amount": 20000})
tasks = engine.create_task_query(process_instance_id=pi.id)  # [人工审批]
engine.complete_task(tasks[0].id, {"approved": True})       # -> COMPLETED
```

**持久化版（M2）**——每个命令边界全量同步 ACT 表；进程崩溃后 `from_database` 恢复：

```python
from camunda.persistence.store import Store

# 裸文件路径即可，Store 自动归一化为 sqlite:/// 绝对路径
engine = ProcessEngine(store=Store("/tmp/camunda.db"))
engine.deploy(model)
pi = engine.start_process_instance_by_key("loan-approval", {"amount": 20000})
# …… 进程在此崩溃 ……

# 重启：恢复部署定义 + 运行中实例（execution 树/task/变量/join 等待登记）
engine2 = ProcessEngine.from_database("/tmp/camunda.db")
pi2 = engine2.get_process_instance(pi.id)
engine2.register_delegate("checkCredit", lambda v: v.update(credit_ok=True))  # delegate 不落库
engine2.complete_task(engine2.create_task_query(process_instance_id=pi.id)[0].id, {"approved": False})
```

**作业执行器版（M3）**——定时启动/定时等待/async 拆分 + 失败重试，`JobExecutor` 后台轮询
（也可手动 `execute_due_jobs()` / 单步 `tick()` 拨钟测试）：

```python
from camunda.engine import ProcessEngine
from camunda.job import JobExecutor

engine = ProcessEngine()
engine.deploy(model)               # 流程含 timer start / timer catch / camunda:asyncBefore 节点
engine.create_job_query()          # [timer-start job(定义级)] 等

ex = JobExecutor(engine, poll_interval=0.5)  # 后台轮询到期作业（用法见 examples/run_timer_demo.py）
ex.start()                                   # 也可 ex.tick() 单步 / ex.shutdown() 停表
# 到期自动：timer-start 启动流程、timer-catch 继续流转、async 执行节点行为；
# 失败自动 retries-1 并顺延 duedate，耗尽即死信；引擎时间可注入 clock 便于测试
```

**边界事件 / asyncAfter（M4-1）**——宿主等待期内 timer 到期中断，或行为完成后异步离开：

```python
# BPMN：<bpmn:userTask id="approve"/> + 平级 <bpmn:boundaryEvent id="timeout"
#       attachedToRef="approve"><bpmn:timerEventDefinition><bpmn:timeDuration
#       xsi:type="bpmn:tFormalExpression">PT2S</bpmn:timeDuration>...
pi = engine.start_process_instance_by_key("approval-timeout")   # 停在审批任务
# 2 秒无人 complete -> JobExecutor 触发 timer-boundary：取消审批任务，token 沿
# 边界事件出边走超时路径；期间 complete_task 则正常走主路、边界作业自动撤销
# asyncAfter：<bpmn:serviceTask camunda:asyncAfter="true" .../> delegate 同步执行，
# 「离开推进」拆成 async-after 作业（XOR 离开时重新求值条件）；asyncBefore+asyncAfter 可链式
```

**内嵌子流程（M4-2a）**——token 进入 subProcess 展开为 SCOPE，内部走完自动收束复活：
```python
# BPMN：<bpmn:subProcess id="fulfill"> 内含独立 startEvent/task/endEvent 与连线
pi = engine.start_process_instance_by_key("order-fulfillment")  # 停在子流程内部质检任务
# - 容器递归解析：跨容器连线引用部署即报错；嵌套子流程逐层收束
# - 边界 timer 挂 subProcess：等待窗口 = 整段内部执行；到期中断整段 scope（内部
#   任务归档/actinst 结算/作业清理），token 沿边界路径走；正常走完则边界撤销
# - 变量沿用实例级（文档化差异）；asyncBefore 可用于 subProcess（展开前异步窗口）
```

**非中断式边界事件（M4-2b4，cancelActivity=false）**——到期不打断宿主，spawn 并发线：
```python
# BPMN：<bpmn:userTask id="handle"/> + <bpmn:boundaryEvent id="esc"
#       attachedToRef="handle" cancelActivity="false"> <timer PT2S ...>
pi = engine.start_process_instance_by_key("ticket-support")  # 停处理任务
# 2 秒无人处理 -> JobExecutor 触发 NI 边界：处理任务保留不取消（cancelActivity=false），
# 并发线从边界出边推进（如自动催办）后收束；宿主随后完成 -> 实例收束完成。
# 若主线先到 end 而并发线（如复核任务）未收束：实例不提前结束，root 转 SCOPE 停驻，
# 等并发线全部收束后实例才完成；subProcess 宿主 + NI 明确报错（文档化差异）
```

**多实例（M4-2c）**——MultiInstanceLoopCharacteristics，三种宿主 userTask/serviceTask/
subProcess（并行/顺序）：
```python
# BPMN：<bpmn:userTask id="review"> <bpmn:multiInstanceLoopCharacteristics
#       camunda:collection="${reviewers}" camunda:elementVariable="reviewer"/>
pi = engine.start_process_instance_by_key("mi-flow", {"reviewers": ["a", "b", "c"]})
# 并行：N 实例任务并存（execution 树 = SCOPE 容器 + N child）；顺序：同一时刻仅 1 实例。
# serviceTask 宿主 = 同步 delegate 无等待窗口（start 返回即收束）；
# subProcess 宿主 = 实例各自进内部流转，内部走完由收束链驱动实例完成、容器收束离开。
# completionCondition（如 ${nrOfCompletedInstances >= 2}）：满足即终止剩余实例（任务归档/
# actinst 结算/整树清理）；空集合零实例直接通过；loopCounter/elementVariable 行为期注入、
# 容器收尾清理；宿主组合 asyncBefore/边界事件运行时明确报错（文档化差异）。
```

**消息 / 信号事件（M4-2d）**——correlate_message 1:1 精准关联，throw_signal 跨实例广播：
```python
# BPMN：<bpmn:intermediateCatchEvent><bpmn:messageEventDefinition messageRef="M1"/>
#       配套 <bpmn:message id="M1" name="orderReady"/> 声明（signal 同型）
pi = engine.start_process_instance_by_key("order-relay")   # 停在消息 catch 停等
engine.correlate_message("orderReady", variables={"paid": True})  # 1:1 投递（未限定实例取注册序最早）
# 边界事件：<bpmn:boundaryEvent attachedToRef="ut" cancelActivity="false">
#           <bpmn:signalEventDefinition signalRef="S1"/>
hits = engine.throw_signal("maintenance")  # 跨实例广播全部命中订阅，返回命中数
# 非中断式边界/事件子流程 start：订阅常驻可重复触发；中断式：触发即消费、宿主被接管
# 实例内 throw：<bpmn:intermediateThrowEvent>/<bpmn:endEvent> + message/signal 定义，
#   触发同实例 catch（未命中静默丢弃）；订阅为纯内存派生态，崩溃恢复自动重推导
```

**DMN 决策表（M5）**——独立决策引擎 + BPMN businessRuleTask 集成：

```python
from camunda.dmn.engine import DmnEngine
from camunda.parser.dmn_parser import parse_dmn_file

dmn = DmnEngine()
dmn.deploy(parse_dmn_file("examples/loan-grading.dmn"))          # -> ["loan-grading", "rate-discount"]
dmn.evaluate_decision("loan-grading", {"amount": 9000, "credit_score": 750})   # -> "B"

# BPMN 集成：<bpmn:businessRuleTask id="grade" camunda:decisionRef="loan-grading"
#            camunda:resultVariable="grade"/>
engine.deploy_dmn(parse_dmn_file("examples/loan-grading.dmn"))
engine.deploy(parse_bpmn_file("examples/loan-grading-flow.bpmn"))
pi = engine.start_process_instance_by_key("loan-process", {"amount": 20000, "credit_score": 500})
pi.variables["grade"]     # "C" -> 决策结果写入 resultVariable，可驱动排他网关选路
```

- **FEEL 子集**（unaryTests 输入单元格）：比较 `= != < <= > >=`、区间 `[a..b]` / `(a..b)` /
  `]a..b[`（DMN 双标记法）、逗号列表 = OR、`not(...)`、`null` 判缺变量、空文本 / `-` = 通配；
  expression（输出单元格 / inputExpression）支持字面量、变量引用（未定义 -> null）、
  算术 `+ - * /` 与括号、字符串 `+` 拼接。不支持函数调用 / between / in / 日期时间 / 路径表达式
  （运行时明确报错）。
- **hitPolicy**：`UNIQUE`（多行命中运行时报错）/ `FIRST` / `ANY`（各行输出不一致报错）/
  `PRIORITY`（按 output 的 outputValues 优先级序取最高，仅单输出列）/ `RULE ORDER`（行结果列表）/
  `COLLECT`（列表，可叠 `SUM` / `MIN` / `MAX` / `COUNT` 聚合）。无命中不报错，返回空结果。
- **已知限制**：DMN 部署不落库（对齐 delegate 注册先例）——崩溃恢复后须重新 `deploy_dmn`，
  否则 businessRuleTask 求值报未部署。

**REST API（M6）**——FastAPI 对齐 Camunda engine-rest，总前缀 `/engine-rest`：

```bash
pip install -e ".[api]"
uvicorn camunda.api.app:create_app --factory --port 8080
# 交互式文档：http://127.0.0.1:8080/docs
```

```python
from camunda.api import create_app
app = create_app()                    # 内存引擎（demo/测试）
app = create_app(engine=engine)       # 复用既有引擎（含 Store / JobExecutor）
```

```bash
# 部署（multipart 对齐 Camunda；另有 JSON 便捷通道 POST /deployment/create/xml）
curl -F "data=@examples/loan-approval.bpmn" \
     http://localhost:8080/engine-rest/deployment/create

# 启动实例（变量兼容包装形态与裸值）
curl -XPOST http://localhost:8080/engine-rest/process-instance \
     -H 'Content-Type: application/json' \
     -d '{"definitionKey":"loan-approval","variables":{"amount":{"value":20000,"type":"Long"}}}'

# 任务：列表 -> 认领 -> 完成
curl http://localhost:8080/engine-rest/task
curl -XPOST http://localhost:8080/engine-rest/task/{id}/claim \
     -H 'Content-Type: application/json' -d '{"userId":"lisi"}'
curl -XPOST http://localhost:8080/engine-rest/task/{id}/complete \
     -H 'Content-Type: application/json' -d '{"variables":{"approved":true}}'
```

| 端点 | 方法 | 说明 |
|---|---|---|
| `/deployment/create` | POST | multipart 部署（字段名 `data`，BPMN/DMN 自动分派） |
| `/deployment/create/xml` | POST | JSON 便捷部署（本项目扩展） |
| `/deployment` | GET | 部署列表 |
| `/process-definition[ /key/{key}[/xml] ]` | GET | 流程定义列表 / 单个 / XML |
| `/process-instance` | POST/GET | 启动 / 列表（按 key、businessKey、active 过滤） |
| `/process-instance/{id}` | GET/DELETE | 查询 / 删除（历史保留，HI 置 DELETED） |
| `/process-instance/{id}/variables[ /{name} ]` | GET/PUT | 变量读写 |
| `/task` | GET | 任务列表（按 assignee / candidateUser / unassigned 过滤） |
| `/task/{id}` | GET | 任务详情 |
| `/task/{id}/claim` `unclaim` `assignee` `complete` | POST | 认领 / 取消 / 指派 / 完成 |
| `/history/process-instance` `task` `activity-instance` `variable-instance` | GET | 四类历史查询 |
| `/decision-definition[ /key/{key} ]` | GET | 决策定义列表 / 单个 |
| `/decision-definition/key/{key}/evaluate` | POST | 决策表求值 |

- **变量形态**：入参兼容包装形态 `{"amount": {"value": 20000}}` 与裸值 `{"amount": 20000}`；
  出参默认包装形态（对齐 Camunda），带 `?bare=true` 退化为裸值 map。

**REST 列表分页（M8）**——9 个列表端点统一支持 `firstResult` + `maxResults`（对齐 Camunda 7 REST）：

```bash
# 翻页：先 0..2，再 2..4，最后 4..200
curl 'http://localhost:8080/engine-rest/process-instance?firstResult=0&maxResults=2'
curl 'http://localhost:8080/engine-rest/process-instance?firstResult=2&maxResults=2'
curl 'http://localhost:8080/engine-rest/process-instance?firstResult=4&maxResults=200'
# 末页判定：返回数 < maxResults 即末页（响应仍为裸数组，不包 count/total）
# 非法 firstResult（<0）/ maxResults（<1）由 FastAPI 422 拒绝
# maxResults > 1000 自动 clamp 到 1000（防脚本误用拉空内存）
```

支持的端点：`/process-instance`、`/task`、`/process-definition`、`/decision-definition`、
`/deployment`、`/history/process-instance`、`/history/task`、`/history/activity-instance`、
`/history/variable-instance`（共 9 个，统一默认 `maxResults=200`）。
- **错误响应体**：统一 `{"type": "<异常类名>", "message": "<异常消息>"}`；
  状态码 —— 未找到 404、部署/参数非法 400、实例状态冲突 409、其余引擎异常 500。

**多 JobExecutor 抢锁（M7）**—— DB CAS lease 模式，多节点 / 多进程安全并发：

```python
from camunda.engine.process_engine import ProcessEngine
from camunda.job.executor import JobExecutor
from camunda.persistence.store import Store

engine = ProcessEngine(store=Store("sqlite:///shared.db"))
engine.deploy(model)

exec_a = JobExecutor(engine, name="exec-a", lease_seconds=300)
exec_b = JobExecutor(engine, name="exec-b", lease_seconds=300)
exec_a.start()      # 后台轮询
exec_b.start()      # 同一个 engine + store，但 lock_owner 不同
# engine.execute_due_jobs(lock_owner=...) 由 JobExecutor.tick() 自动注入
```

- **抢锁语义**：JobExecutor 启动时按 `name-pid-hostname-uuid8` 生成唯一
  `lock_owner`，调 `engine.execute_due_jobs(lock_owner=me, lease_seconds=300)`
  —— 引擎走 `_execute_due_jobs_db`，从 `store.acquire_due_jobs` 拿一批
  due job：每条 `UPDATE ACT_RU_JOB SET LOCK_OWNER=me, LOCK_EXP_TIME=now+lease
  WHERE id=:id AND (LOCK_OWNER_ IS NULL OR LOCK_EXP_TIME_ < :now)`，affected_rows > 0
  即抢到。
- **崩溃恢复**：JobExecutor.shutdown 不主动释放 lease（让其自然过期），
  便于模拟崩溃场景；其他 JobExecutor 等 lease 过期后 `LOCK_EXP_TIME_ < :now`
  命中，可重新抢到。
- **续约**：长作业执行期间调 `store.extend_lock(job_id, owner, lease_seconds, now)`
  把 lease 延后；CAS 失败 = 锁已被别人接管，当前执行应中止提交。
- **监控**：`store.list_locks(lock_owner=None)` 查看当前持锁情况（调试 / 运维）。
- **兼容性**：engine 无 Store 时 JobExecutor 自动走单进程内存路径（向后兼容旧用法）。

## 里程碑

| 版本 | 内容 | 状态 |
|---|---|---|
| M0 | 骨架 + 模型 + 解析器 | ✅ |
| M1 | 内存版流转（网关/任务/服务任务） | ✅ |
| M2 | SQLAlchemy 持久化 + 崩溃恢复（ACT_RE/RU/HI） | ✅ |
| M3 | 作业执行器（Timer / async continuation） | ✅ |
| M4 | BPMN 全元素补齐 | 🔄 M4-1 边界事件 + asyncAfter、M4-2a 内嵌子流程、M4-2b 事件子流程 + 非中断式边界、M4-2c 多实例三宿主、M4-2d 消息/信号事件已交付 |
| M5 | DMN + FEEL 子集 | ✅ 决策表解析 + FEEL 子集求值 + 六种 hitPolicy 收敛 + businessRuleTask 集成已交付 |
| M6 | REST API（FastAPI） | ✅ 六类端点（部署/定义/实例/任务/历史/决策）+ 变量双形态 + 异常映射已交付 |
| M7 | 多 JobExecutor 抢锁 | ✅ DB CAS lease：跨 JobExecutor / 跨进程唯一执行同一作业 + lease 过期可被接管 + 续约已交付 |
| M8 | REST 列表分页 | ✅ 9 个列表端点统一 firstResult + maxResults（对齐 Camunda 7 REST），maxResults 默认 200 / 硬上限 1000 |
