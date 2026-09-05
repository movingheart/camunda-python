# camunda-python 使用说明

> 拿到代码不知道从哪下口？这份文档按**教程**节奏带你从「跑通 hello world」走到「生产部署」。读完之后你应该能：
>
> - 自己写一份简单的 BPMN 并用 Python 引擎跑起来
> - 知道什么时候该开持久化、什么时候该起 JobExecutor
> - 遇到 90% 的常见坑能秒定位
>
> 想看「这个项目覆盖了哪些 BPMN 特性」看 [README.md](../README.md)；想看「为什么这样设计 / 与 Camunda 7 有何差异」看 [docs/ARCHITECTURE.md](ARCHITECTURE.md)。

---

## 目录

1. [项目是什么 / 不是什么](#1-项目是什么--不是什么)
2. [安装](#2-安装)
3. [教程 1：5 分钟跑通 hello world](#3-教程-15-分钟跑通-hello-world)
4. [教程 2：BPMN 文件怎么写](#4-教程-2bpmn-文件怎么写)
5. [教程 3：网关与条件](#5-教程-3网关与条件)
6. [教程 4：用户任务的人工流转](#6-教程-4用户任务的人工流转)
7. [教程 5：服务任务与 delegate](#7-教程-5服务任务与-delegate)
8. [教程 6：持久化与崩溃恢复](#8-教程-6持久化与崩溃恢复)
9. [教程 7：定时器与作业执行器](#9-教程-7定时器与作业执行器)
10. [教程 8：边界事件 / asyncAfter / 子流程 / 事件子流程](#10-教程-8边界事件--asyncafter--子流程--事件子流程)
11. [教程 9：多实例](#11-教程-9多实例)
12. [教程 10：消息 / 信号事件](#12-教程-10消息--信号事件)
13. [教程 11：DMN 决策表 + businessRuleTask](#13-教程-11dmn-决策表--businessruletask)
14. [教程 12：REST API](#14-教程-12rest-api)
15. [教程 13：多进程 JobExecutor 部署](#15-教程-13多进程-jobexecutor-部署)
16. [常见坑 FAQ](#16-常见坑-faq)
17. [调试技巧](#17-调试技巧)
18. [生产部署清单](#18-生产部署清单)

---

## 1. 项目是什么 / 不是什么

**是什么**

- 一个 **Apache-2.0** 开源的 Python 3 BPMN / DMN 引擎，**对齐 Camunda 7 的运行时语义**（不是抄 Java 源码，是按官方文档语义在 Python 重实现）。
- 自带 SQLAlchemy 2.0 持久化（默认 SQLite，生产可换 PostgreSQL）、可选 FastAPI REST 层（路径前缀 `/engine-rest`，对齐 Camunda 7 REST）。
- 支持 BPMN 全家桶：start/end event、网关（排他/并行/包容）、用户任务、服务任务、脚本任务、businessRuleTask、子流程、事件子流程、多实例（userTask/serviceTask/subProcess）、边界事件（中断/非中断）、消息/信号事件、定时器事件。
- DMN 决策表 + FEEL 子集（unaryTests + expression），六种 hitPolicy 全支持。

**不是什么**

- **不是 Camunda 平台的复刻**——没有 web 控制台（Camunda Cockpit / Tasklist / Admin），没有 REST 鉴权，没有历史级别数据可视化。
- **不是工作流引擎的「开箱即用替代」**——它是给「想用 Camunda 语义但要 Python 生态」的人用的胶水层；你大概率要自己写 web 前端 / 任务中心。
- **不是 SaaS**——本地进程内运行，多副本需要手动共享 Store（详见教程 13）。
- **不是 Camunda 7 的全功能 parity**——本项目是「语义对齐」，部分高级特性会标出已知差异（见 FAQ）。

---

## 2. 安装

### 2.1 基本环境

- Python **3.12+**（用到了 PEP 695 类型参数化语法）
- pip / venv
- 操作系统无关（开发在 macOS，CI 在 Linux 都跑过；纯 Python，无 C 扩展）

### 2.2 本地开发装

```bash
git clone <your-fork-url> camunda-python
cd camunda-python
python3 -m venv .venv
source .venv/bin/activate         # Windows: .venv\Scripts\activate
pip install -e ".[dev]"           # 含 pytest + pytest-cov
pytest                            # 268 个单测应全过
```

### 2.3 加 REST API 依赖

```bash
pip install -e ".[api]"          # 追加 fastapi/uvicorn/python-multipart/httpx
```

### 2.4 全装

```bash
pip install -e ".[dev,api]"
```

### 2.5 切换到 PostgreSQL（生产）

引擎默认 SQLite（`sqlite:///...`）。换 PG 只需在构造 `Store` 时改 URL：

```python
from camunda.persistence.store import Store
store = Store("postgresql+psycopg://user:pwd@host:5432/camunda")
```

表结构由 SQLAlchemy 自动建（`Base.metadata.create_all`），首次启动会建好 ACT_RE/RU/HI 三套表。**注意**：

- 当前 ORM 用的是 SQLite-friendly 类型映射，**切 PG 后建议自己跑一轮 `tests/` 确认无字段类型问题**（尤其 `Numeric` / `Boolean` 的边缘场景）。
- 生产强烈建议加 `pool_pre_ping=True` 防止 PG 断连后僵尸连接。

---

## 3. 教程 1：5 分钟跑通 hello world

我们跑一份最简流程：start → service task（打印 "hi"）→ end。三个节点，零配置。

### 3.1 准备 BPMN 文件

新建 `hello.bpmn`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:bpmn2="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  id="Definitions_1"
                  targetNamespace="http://bpmn.io/schema/bpmn">

  <bpmn:process id="hello" name="Hello Process" isExecutable="true">
    <bpmn:startEvent id="start" name="Start"/>
    <bpmn:serviceTask id="greet" name="Greet" camunda:delegateExpression="${greeter}">
      <bpmn:incoming>Flow_1</bpmn:incoming>
      <bpmn:outgoing>Flow_2</bpmn:outgoing>
    </bpmn:serviceTask>
    <bpmn:endEvent id="end" name="End">
      <bpmn:incoming>Flow_2</bpmn:incoming>
    </bpmn:endEvent>

    <bpmn:sequenceFlow id="Flow_1" sourceRef="start" targetRef="greet"/>
    <bpmn:sequenceFlow id="Flow_2" sourceRef="greet" targetRef="end"/>
  </bpmn:process>

  <bpmn:message id="Message_1" name="helloMsg"/>
</bpmn:definitions>
```

**等等**，你看到 `camunda:delegateExpression` 前缀——这是 Camunda 的命名空间，需要加到 `<definitions>` 上。补上 `xmlns:camunda="http://camunda.org/schema/1.0/bpmn"` 后完整头是：

```xml
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
                  id="Definitions_1"
                  targetNamespace="http://bpmn.io/schema/bpmn">
```

> 没加也能跑——本项目解析器容忍缺省命名空间（会自动 fallback），但**生产建议显式声明**所有用到的命名空间，避免和不同建模工具导出的 XML 不兼容。

### 3.2 写 Python 脚本

新建 `run_hello.py`：

```python
from camunda.parser import parse_bpmn_xml
from camunda.engine import ProcessEngine

# 1) 解析 BPMN
with open("hello.bpmn") as f:
    model = parse_bpmn_xml(f.read(), source_name="hello.bpmn")

# 2) 起引擎 + 注册 delegate（service task 实际干活的 Python 函数）
engine = ProcessEngine()
engine.register_delegate("greeter", lambda variables: variables.update(greeting="hi from python"))

# 3) 部署 + 启动实例 + 同步跑完
engine.deploy(model)
pi = engine.start_process_instance_by_key("hello", {"name": "world"})
print("instance:", pi.id, "vars:", pi.variables)
assert pi.is_completed
```

### 3.3 跑

```bash
python run_hello.py
# 输出：instance: <uuid> vars: {'name': 'world', 'greeting': 'hi from python'}
```

### 3.4 完整流程时序

```
start  ──>  service task (greeter delegate)  ──>  end
   │              │
   │              └─ delegate 修改 variables：注入 greeting
   └─ instance 进入 RUNNING
   ────────────────> instance 进入 COMPLETED
```

如果 `assert pi.is_completed` 失败，要么 delegate 没注册对名字（`register_delegate("greeter", ...)`），要么 BPMN 里 `delegateExpression="${greeter}"` 的名字跟注册名不一致。**先看错误堆栈**，常见原因见 FAQ §16.1。

---

## 4. 教程 2：BPMN 文件怎么写

### 4.1 命名约定

| 字段 | 作用 | 不能重名？ | 不能为空？ |
|---|---|---|---|
| `<process id="...">` | 流程 key（`start_process_instance_by_key()` 的参数） | 全局唯一 | 必填 |
| `<process name="...">` | 显示名，UI 用 | 可重名 | 可空 |
| `<bpmn:* id="...">` | 节点 id（引擎内部引用、日志、actinst 都用） | 流程内唯一 | 必填 |
| `<sequenceFlow id="...">` | 连线 id | 流程内唯一 | 必填 |

### 4.2 命名空间最小集合

```xml
<bpmn:definitions xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:bpmn2="http://www.omg.org/spec/BPMN/20100524/MODEL"
                  xmlns:camunda="http://camunda.org/schema/1.0/bpmn"
                  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
                  id="Definitions_1"
                  targetNamespace="http://bpmn.io/schema/bpmn">
```

> 历史包袱：很多导出工具同时输出 `bpmn:` 和 `bpmn2:` 两个命名空间。本项目解析器容忍两者等价，但建议**只用 `bpmn:`**。

### 4.3 节点必备字段

- 每个 `FlowNode` 必须有 `id`
- 节点间连线用 `incoming` / `outgoing` 显式声明（不显式也跑得起来，但**强烈建议都写**——可视化工具靠它渲染）
- start event 不能有 `incoming`，end event 不能有 `outgoing`

### 4.4 表达式（FEEL/JUEL 子集）

`camunda:` 命名空间下的 `*Expression` 字段（`camunda:expression`、`camunda:delegateExpression`、网关条件、`multiInstance` collection 等）都走 JUEL 风格 `${...}`，本项目支持：

- **字面量**：字符串 `'hello'` / 数字 `42` / 布尔 `true` / `null`
- **变量引用**：`${amount}`、`${applicant.name}`（不支持深层路径求值）— 找不到返回 `null`
- **算术**：`+ - * / %`、`(...)` 优先级
- **比较**：`== != < <= > >=`
- **逻辑**：`&& || !`
- **字符串拼接**：`${greeting + ", " + name}`

**不支持**：函数调用、`between`、`in`、`matches`、日期时间字面量、列表字面量 `[1,2,3]`（多实例的 collection 例外，专门解析）。

### 4.5 推荐画图工具

| 工具 | 兼容性 | 推荐度 |
|---|---|---|
| Camunda Modeler | 100%（本项目就是它的语义） | ⭐⭐⭐⭐⭐ |
| bpmn.io Modeler | 同 Camunda Modeler（基于 bpmn-js） | ⭐⭐⭐⭐⭐ |
| draw.io | 命名空间偶有异常 | ⭐⭐ |
| Visio | 不推荐，导出会丢属性 | ❌ |

---

## 5. 教程 3：网关与条件

### 5.1 排他网关（exclusiveGateway）

按条件选**唯一**一条出边执行：

```xml
<bpmn:exclusiveGateway id="gw"/>
<bpmn:sequenceFlow id="to_high"  sourceRef="gw" targetRef="high">
  <bpmn:conditionExpression xsi:type="bpmn:tFormalExpression">${amount > 10000}</bpmn:conditionExpression>
</bpmn:sequenceFlow>
<bpmn:sequenceFlow id="to_low"   sourceRef="gw" targetRef="low"/>
```

**语义**：从上到下评估，第一个为 `true` 的出边执行；都没有就抛 `ProcessEngineException`（「No outgoing sequence flow satisfies the condition」）。

完整例子见 [`examples/loan-approval.bpmn`](../examples/loan-approval.bpmn)。

### 5.2 并行网关（parallelGateway）

**分叉**：每条出边都 spawn 一条 token，并行推进。
**汇合**：等所有入边的 token 都到才激活后继。

```xml
<bpmn:parallelGateway id="fork"/>
<bpmn:parallelGateway id="join"/>
<bpmn:sequenceFlow id="f1" sourceRef="fork" targetRef="t1"/>
<bpmn:sequenceFlow id="f2" sourceRef="fork" targetRef="t2"/>
<bpmn:sequenceFlow id="j1" sourceRef="t1" targetRef="join"/>
<bpmn:sequenceFlow id="j2" sourceRef="t2" targetRef="join"/>
```

**注意 token 计数**——并行网关的汇合必须等齐所有 token，少一条会卡死。调试时查 `engine.create_execution_query()` 看哪些 execution 在等。

### 5.3 包容网关（inclusiveGateway）

排他的并集：评估所有为 `true` 的出边，可能激活 1 条或多条。汇合同样要等齐。

完整例子见 [`examples/parallel-review.bpmn`](../examples/parallel-review.bpmn)（但注意这是并行网关版本）。

---

## 6. 教程 4：用户任务的人工流转

### 6.1 完整形态

```xml
<bpmn:userTask id="approve" name="Approve Request"
               camunda:assignee="${approver}"
               camunda:candidateUsers="${reviewers}"
               camunda:formKey="app:approve-form"/>
```

| 字段 | 含义 | 何时用 |
|---|---|---|
| `camunda:assignee` | 直接指派人（执行人）；表达式求值为用户名 | 单人单签 |
| `camunda:candidateUsers` | 候选用户列表（逗号分隔） | 多人抢单 |
| `camunda:candidateGroups` | 候选组列表 | 按部门 / 角色派单 |
| `camunda:formKey` | 关联表单模板 key | 配套 UI 渲染表单 |

### 6.2 引擎操作

```python
# 查我的待办
tasks = engine.create_task_query(assignee="alice")

# 查候选池
tasks = engine.create_task_query(candidate_user="bob")

# 查未认领
tasks = engine.create_task_query(unassigned=True)

# 完成（带出参）
engine.complete_task(tasks[0].id, variables={"approved": True, "comment": "looks good"})

# 改指派人
engine.set_variable(tasks[0].id, "assignee", "carol")  # 通过 task id 路由
# 或通过 service task 风格的 claim/unclaim
engine.claim_task(task_id, user_id="bob")
engine.unclaim_task(task_id)
```

### 6.3 候选 → assignee 的提升（claim）

候选用户池里任一人都能 claim 到自己名下。**实现路径**：

```python
# 1. 用 service task 风格的 claim
engine.claim_task(task_id, user_id="bob")

# 2. 等价于：在 task 上设变量 assignee = "bob"
engine.set_variable(task_id, "assignee", "bob")
```

两种写法等价，REST API 都暴露（`POST /task/{id}/claim`）。

---

## 7. 教程 5：服务任务与 delegate

### 7.1 delegate 三种写法

#### 写法 A：`delegateExpression`（推荐）

```xml
<bpmn:serviceTask id="sendMail" camunda:delegateExpression="${send_mail_delegate}"/>
```

```python
def send_mail_delegate(variables):
    to = variables.get("email")
    body = variables.get("body")
    send_email(to, body)             # 你的真实业务调用

engine.register_delegate("send_mail_delegate", send_mail_delegate)
```

#### 写法 B：`expression`（内联）

```xml
<bpmn:serviceTask id="compute" camunda:expression="${exec(compute_service, variables)}"/>
```

`expression` 直接求值——本项目把它当 EL 求值，结果丢弃。**实务中 99% 场景用 `delegateExpression` 更清晰**。

#### 写法 C：Java class（不支持）

本项目无 Java 桥接，需要自定义逻辑时**必须用 `delegateExpression` + Python 函数**。

### 7.2 delegate 签名

```python
def my_delegate(variables: dict) -> None:
    """读 variables / 修改 variables / 调外部服务
       返回值会被忽略（除非你想用 expression 形式）"""
```

**变量就地修改就够用**。如果需要「新增流程实例级副作用」（如发通知），从 delegate 直接调外部系统，**别绕回去改引擎状态**——会让持久化版本陷入循环。

### 7.3 同步 vs 异步

默认 `serviceTask` **同步**执行——delegate 返回后立刻推进 token。

```xml
<!-- async 写法 -->
<bpmn:serviceTask id="heavy" camunda:delegateExpression="${heavy}"
                  camunda:asyncBefore="true" camunda:asyncAfter="true"/>
```

`asyncBefore`：进入节点前先入队，由 JobExecutor 拉起执行。
`asyncAfter`：节点行为执行完后，「离开推进」入队，由 JobExecutor 拉起。

**什么时候用 async**：

- delegate 慢（>1s）— 不阻塞 API 响应
- 需要重试语义——async 作业失败自动 retries-1 顺延
- 需要分布式执行（多 JobExecutor 抢锁）

完整 async 例子见 [`examples/timer-billing.bpmn`](../examples/timer-billing.bpmn)。

---

## 8. 教程 6：持久化与崩溃恢复

### 8.1 何时需要持久化

| 场景 | 需要？ | 原因 |
|---|---|---|
| Demo / 单次脚本 | ❌ | 跑完就扔 |
| 长期运行的流程（>1h）| ✅ | 进程崩了不能从头来 |
| 多副本部署 | ✅ 必须 | 共享 Store 才能协调 |
| 定时器 / 边界事件 | ✅ | 进程重启后 timer 不丢 |
| async continuation | ✅ | 重试依赖持久化的 job |

### 8.2 启用方式

```python
from camunda.engine import ProcessEngine
from camunda.persistence.store import Store

engine = ProcessEngine(store=Store("/path/to/camunda.db"))   # SQLite
# 或：engine = ProcessEngine(store=Store("postgresql+psycopg://..."))
engine.deploy(model)
```

### 8.3 崩溃恢复

```python
# 重启进程，从 DB 加载全部状态
engine = ProcessEngine.from_database("/path/to/camunda.db")

# 重新注册 delegate（DB 不存 delegate 实现！）
engine.register_delegate("send_mail", send_mail_fn)

# 继续推进
tasks = engine.create_task_query()
engine.complete_task(tasks[0].id, variables={...})
```

### 8.4 ⚠️ delegate 不落库（重大陷阱）

**DB 里只存 process 定义、execution、task、变量、job。delegate 函数本身必须每次启动进程重新 `register_delegate()`。**

> 这与 Camunda 7 一致（Java 类不会入库，靠 classloader 解析）。本项目同理——如果你看到「服务任务卡住不前进」，90% 是 delegate 没注册。

**生产建议**：把 delegate 注册集中到一个 `register_all(engine)` 函数，启动时统一调用：

```python
# delegates.py
def register_all(engine):
    engine.register_delegate("send_mail", send_mail)
    engine.register_delegate("charge_card", charge_card)
    engine.register_delegate("grade", grade_loan)

# app.py
engine = ProcessEngine.from_database(DB_URL)
register_all(engine)
```

### 8.5 持久化版本的速度代价：每个命令边界全量同步 ACT 表

不是 lazy flush——`start_process_instance` 后立刻 3 张表 INSERT/UPDATE。一次命令可能 10~50 个 SQL。

测试时如果嫌慢，可以用 `ProcessEngine()`（无 Store）跑用例，最后再用持久化版跑一轮集成。

---

## 9. 教程 7：定时器与作业执行器

### 9.1 三种定时器

```xml
<!-- start event 上：定时启动 -->
<bpmn:startEvent id="start">
  <bpmn:timerEventDefinition>
    <bpmn:timeCycle>0/5 * * * * ?</bpmn:timeCycle>   <!-- cron：每 5 秒 -->
  </bpmn:timerEventDefinition>
</bpmn:startEvent>

<!-- intermediate catch event：流程中等候 -->
<bpmn:intermediateCatchEvent id="wait">
  <bpmn:timerEventDefinition>
    <bpmn:timeDuration>PT10S</bpmn:timeDuration>     <!-- ISO 8601 duration：等 10 秒 -->
  </bpmn:timerEventDefinition>
</bpmn:intermediateCatchEvent>

<!-- boundary event 上：宿主超时 -->
<bpmn:boundaryEvent id="timeout" attachedToRef="approve">
  <bpmn:timerEventDefinition>
    <bpmn:timeDate>2026-12-31T23:59:59+08:00</bpmn:timeDate>  <!-- 绝对时间 -->
  </bpmn:timerEventDefinition>
</bpmn:boundaryEvent>
```

| 形式 | 含义 | 用例 |
|---|---|---|
| `timeDate` | 绝对时间 | 截止日期 |
| `timeDuration` | ISO 8601 时长（PT5M / P1D / PT2H30M） | 等 N 秒 / N 天 |
| `timeCycle` | cron 表达式或 R5/PT10S 重复 | 每 5 秒重试 / 每天 9 点 |

### 9.2 JobExecutor 用法

```python
from camunda.job import JobExecutor

ex = JobExecutor(engine, poll_interval=0.5, lease_seconds=300)
ex.start()      # 启动后台线程
# ... 流程正常推进，定时器到时自动执行 ...
ex.shutdown()    # 停轮询，**不主动释放锁**（让 lease 自然过期，便于模拟崩溃）
```

也可以手动驱动（测试用）：

```python
ex.tick()        # 拉一批 due job 同步执行
ex.execute_due_jobs()  # 等价于 tick()
```

### 9.3 时间注入（测试）

```python
from datetime import datetime, timedelta
from unittest.mock import patch

# 让 JobExecutor 看到的时间提前 1 小时
fake_now = datetime.now() + timedelta(hours=1)
with patch.object(engine, "_now", return_value=fake_now):
    ex.tick()
```

完整 demo 见 [`examples/run_timer_demo.py`](../examples/run_timer_demo.py)。

### 9.4 失败重试

```xml
<bpmn:serviceTask id="flaky" camunda:delegateExpression="${flaky}"
                  camunda:asyncBefore="true">
  <bpmn:extensionElements>
    <camunda:FailedJobRetryTimeCycle>R3/PT10S</camunda:FailedJobRetryTimeCycle>
  </bpmn:extensionElements>
</bpmn:serviceTask>
```

- `R3` = 最多重试 3 次
- `PT10S` = 每次间隔 10 秒
- 全部失败 → retries 归零 → 进入「死信」（仍在表里，但不再被 acquire）

---

## 10. 教程 8：边界事件 / asyncAfter / 子流程 / 事件子流程

### 10.1 边界事件（boundaryEvent）

挂在某个 Activity 边上，等宿主期间某种事件触发。

```xml
<bpmn:userTask id="approve"/>
<bpmn:boundaryEvent id="timeout" attachedToRef="approve">
  <bpmn:timerEventDefinition>
    <bpmn:timeDuration>PT2S</bpmn:timeDuration>
  </bpmn:timerEventDefinition>
</bpmn:boundaryEvent>
```

- 默认 `cancelActivity="true"`（中断式）：到时取消宿主任务，token 走边界出边。
- `cancelActivity="false"`（非中断式）：宿主保留，并发线从边界出边推进。详见 §10.3。

完整例子见 [`examples/boundary-approval.bpmn`](../examples/boundary-approval.bpmn)、[`examples/run_boundary_demo.py`](../examples/run_boundary_demo.py)。

### 10.2 内嵌子流程（subProcess）

```xml
<bpmn:subProcess id="fulfill" name="Fulfillment">
  <bpmn:startEvent id="sub_start"/>
  <bpmn:userTask id="inspect" name="Inspect Item"/>
  <bpmn:endEvent id="sub_end"/>
  <bpmn:sequenceFlow id="s1" sourceRef="sub_start" targetRef="inspect"/>
  <bpmn:sequenceFlow id="s2" sourceRef="inspect" targetRef="sub_end"/>
</bpmn:subProcess>
```

语义：token 进 subProcess → 进入内部流转 → 内部走完 → token 自动从 subProcess 出边继续（收束）。

子流程上挂 boundary event：等待窗口 = **整段内部执行时间**，到期中断整段 scope（内部 task 归档、actinst 结算、job 清理）。

完整例子见 [`examples/subprocess-dispatch.bpmn`](../examples/subprocess-dispatch.bpmn)、[`examples/run_subprocess_demo.py`](../examples/run_subprocess_demo.py)。

### 10.3 非中断式边界（cancelActivity="false"）

```xml
<bpmn:userTask id="handle" name="Handle Ticket"/>
<bpmn:boundaryEvent id="esc" attachedToRef="handle" cancelActivity="false">
  <bpmn:timerEventDefinition>
    <bpmn:timeDuration>PT2S</bpmn:timeDuration>
  </bpmn:timerEventDefinition>
</bpmn:boundaryEvent>
```

语义：

- 到时**不**取消宿主 handle，spawn 并发线从 esc 出边推进（如自动催办）
- 宿主完成后正常走主路 → 实例收束完成
- **如果主线先到 end 并发线还没收束**：root 转 SCOPE 停驻，等并发线全部收束后实例才完成

**已知限制**：subProcess 宿主 + 非中断式边界 = 不支持（启动时报错「NI boundary on subProcess not supported」）。

完整例子见 [`examples/ticket-ni-support.bpmn`](../examples/ticket-ni-support.bpmn)、[`examples/run_ni_demo.py`](../examples/run_ni_demo.py)。

### 10.4 事件子流程（eventSubProcess / subprocess triggered by event）

```xml
<bpmn:subProcess id="errorHandler" triggeredByEvent="true">
  <bpmn:startEvent id="err_start">
    <bpmn:errorEventDefinition errorRef="E_ORDER_FAIL"/>
  </bpmn:startEvent>
  <bpmn:serviceTask id="refund" camunda:delegateExpression="${refund}"/>
  <bpmn:endEvent id="err_end"/>
  <bpmn:sequenceFlow id="e1" sourceRef="err_start" targetRef="refund"/>
  <bpmn:sequenceFlow id="e2" sourceRef="refund" targetRef="err_end"/>
</bpmn:subProcess>
```

挂在父流程 scope 上：当父流程内任意节点 `throw_error("E_ORDER_FAIL")`，事件子流程被触发；走完后 token 不返回父流程（子流程的 end 是终态）。

完整例子见 [`examples/run_ni_demo.py`](../examples/run_ni_demo.py)（错误处理部分）。

### 10.5 asyncBefore / asyncAfter（再谈）

回到 §7.3 的简述：

- `asyncBefore`：进节点前入队 → JobExecutor 拉起 → delegate 执行 → 出边推进
- `asyncAfter`：delegate 执行完 → 「离开推进」入队 → JobExecutor 拉起 → 推进

**典型用法**：serviceTask 默认同步；加 `camunda:asyncAfter="true"` 后，XOR 离开时重新求值条件可在另一个事务里跑，便于和其他事件解耦。

---

## 11. 教程 9：多实例

### 11.1 三种宿主

```xml
<bpmn:userTask id="review">
  <bpmn:multiInstanceLoopCharacteristics
       camunda:collection="${reviewers}"
       camunda:elementVariable="reviewer"/>
  <!-- 多实例生成 N 个 review 实例，每个实例的 reviewer 变量为列表中一个元素 -->
</bpmn:userTask>
```

| 宿主 | 行为 |
|---|---|
| userTask | 每个元素一个 task 实例（user 必须逐个 complete） |
| serviceTask | **同步**展开——delegate 立即被调用 N 次，元素变量依次注入 |
| subProcess | 每个元素一个 subProcess 实例，内部流转相互独立 |

### 11.2 completionCondition（提前终止）

```xml
<bpmn:multiInstanceLoopCharacteristics
     camunda:collection="${items}"
     camunda:elementVariable="item"
     camunda:completionCondition="${nrOfCompletedInstances >= 2}"/>
```

满足条件即终止剩余实例（任务归档、actinst 结算、整树清理）。

### 11.3 顺序 vs 并行

默认**并行**——N 个实例同时存活。

```xml
<bpmn:multiInstanceLoopCharacteristics
     camunda:collection="${items}"
     camunda:elementVariable="item"
     camunda:isSequential="true"/>   <!-- 顺序：同一时刻只有 1 个实例存活 -->
```

**已知限制**：

- serviceTask + asyncBefore：serviceTask 是同步展开，asyncBefore 在多实例语义下无效（视为同步）
- subProcess + 边界事件：subProcess 多实例的边界事件不传播 cancelActivity 到全部实例

完整例子见 [`examples/mi-rollout.bpmn`](../examples/mi-rollout.bpmn)、[`examples/run_mi_demo.py`](../examples/run_mi_demo.py)。

---

## 12. 教程 10：消息 / 信号事件

### 12.1 消息（message）—— 1:1 关联

```xml
<bpmn:intermediateCatchEvent id="waitOrder">
  <bpmn:messageEventDefinition messageRef="M_OrderReady"/>
</bpmn:intermediateCatchEvent>
<!-- 在 <definitions> 下声明： -->
<bpmn:message id="M_OrderReady" name="orderReady"/>
```

触发：

```python
# 1:1 投递，未限定实例取最早注册的
engine.correlate_message("orderReady", variables={"paid": True})
```

### 12.2 信号（signal）—— 跨实例广播

```xml
<bpmn:intermediateThrowEvent id="alert">
  <bpmn:signalEventDefinition signalRef="S_Maintenance"/>
</bpmn:intermediateThrowEvent>
<bpmn:message id="..."/>  <!-- signal 在 <definitions> 下声明： -->
<bpmn:signal id="S_Maintenance" name="maintenance"/>
```

```python
hits = engine.throw_signal("maintenance")   # 广播，返回命中数
```

非中断式边界 / 事件子流程 start 上的信号订阅：触发后订阅仍保留，可再次触发。
中断式：触发即消费。

完整例子见 [`examples/msg-sig-relay.bpmn`](../examples/msg-sig-relay.bpmn)、[`examples/msg-sig-broadcast.bpmn`](../examples/msg-sig-broadcast.bpmn)、[`examples/run_msg_sig_demo.py`](../examples/run_msg_sig_demo.py)。

---

## 13. 教程 11：DMN 决策表 + businessRuleTask

### 13.1 写决策表

新建 `grading.dmn`：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<definitions xmlns="https://www.omg.org/spec/DMN/20191111/MODEL/"
             xmlns:dmndi="https://www.omg.org/spec/DMN/20191111/DMNDI/"
             xmlns:dc="http://www.omg.org/spec/DMN/20180521/DC/"
             id="defs_grading" name="Grading" namespace="http://camunda.org/schema/1.0/dmn">

  <decision id="loan_grading" name="Loan Grading">
    <decisionTable id="dt_grading" hitPolicy="FIRST">
      <input id="i_amount">
        <inputExpression id="ie_amount" typeRef="number">
          <text>amount</text>
        </inputExpression>
      </input>
      <input id="i_score">
        <inputExpression id="ie_score" typeRef="number">
          <text>credit_score</text>
        </inputExpression>
      </input>
      <output id="o_grade" name="grade" typeRef="string"/>

      <rule id="r1">
        <inputEntry id="r1_i1"><text>&lt; 10000</text></inputEntry>
        <inputEntry id="r1_i2"><text>&gt;= 700</text></inputEntry>
        <outputEntry id="r1_o1"><text>"A"</text></outputEntry>
      </rule>
      <rule id="r2">
        <inputEntry id="r1_i1"><text>&lt; 10000</text></inputEntry>
        <inputEntry id="r1_i2"><text>[500..700)</text></inputEntry>
        <outputEntry id="r1_o1"><text>"B"</text></outputEntry>
      </rule>
      <rule id="r3">
        <inputEntry id="r1_i1"><text>&gt;= 10000</text></inputEntry>
        <inputEntry id="r1_i2"><text>[700..900]</text></inputEntry>
        <outputEntry id="r1_o1"><text>"A"</text></outputEntry>
      </rule>
    </decisionTable>
  </decision>
</definitions>
```

### 13.2 部署 + 求值

```python
from camunda.parser.dmn_parser import parse_dmn_file
from camunda.dmn.engine import DmnEngine

dmn = DmnEngine()
dmn.deploy(parse_dmn_file("grading.dmn"))
print(dmn.evaluate_decision("loan_grading", {"amount": 9000, "credit_score": 750}))   # "A"
```

### 13.3 BPMN 集成

```xml
<bpmn:businessRuleTask id="grade" name="Grade Loan"
                      camunda:decisionRef="loan_grading"
                      camunda:resultVariable="grade"/>
```

```python
from camunda.engine import ProcessEngine
from camunda.parser import parse_bpmn_xml

engine = ProcessEngine()
engine.deploy_dmn(parse_dmn_file("grading.dmn"))
engine.deploy(parse_bpmn_xml(open("grading-flow.bpmn").read()))

pi = engine.start_process_instance_by_key("loan-process", {"amount": 20000, "credit_score": 500})
print(pi.variables["grade"])   # "C" — 决策结果写入 resultVariable，可驱动排他网关
```

### 13.4 FEEL 速记

| 写法 | 含义 |
|---|---|
| `< 10000` | 比较 |
| `[500..700)` | 区间（含 500 不含 700） |
| `]500..700[` | DMN 双标记（同上） |
| `<500, >800` | OR（逗号分隔） |
| `not(>500)` | 取反 |
| `-` / 空 | 通配（任意值） |

完整 FEEL 子集见 [docs/ARCHITECTURE.md § M5](ARCHITECTURE.md#m5-交付记录)。

### 13.5 hitPolicy 选择

| hitPolicy | 行为 | 用例 |
|---|---|---|
| `UNIQUE` | 多行命中报错 | 严格一对一 |
| `FIRST` | 第一条命中 | 优先级表 |
| `PRIORITY` | 按 output 优先级排序取最高 | 风险评级 |
| `ANY` | 多行命中但输出需一致，否则报错 | 互斥规则 |
| `RULE ORDER` | 返回所有命中行的列表 | 收集多条 |
| `COLLECT` | 列表（可叠 `SUM/MIN/MAX/COUNT` 聚合） | 汇总统计 |

完整例子见 [`examples/loan-grading.dmn`](../examples/loan-grading.dmn)、[`examples/run_dmn_demo.py`](../examples/run_dmn_demo.py)。

---

## 14. 教程 12：REST API

### 14.1 启动服务

```python
# way 1：内置默认引擎（demo / 测试）
from camunda.api import create_app
app = create_app()

# way 2：复用你自己的引擎（含 Store / JobExecutor）
from camunda.engine import ProcessEngine
from camunda.api import create_app
engine = ProcessEngine.from_database("/path/to/db")
app = create_app(engine=engine)
```

```bash
uvicorn camunda.api.app:create_app --factory --port 8080
# 或：
python -m uvicorn camunda.api.app:create_app --factory --port 8080 --reload   # 开发模式
```

打开 `http://127.0.0.1:8080/docs` 看 Swagger UI。

### 14.2 端点清单（27 个）

| 类别 | 方法 | 路径 | 说明 |
|---|---|---|---|
| 部署 | POST | `/deployment/create` | multipart 部署 BPMN/DMN（字段名 `data`，按后缀自动分派） |
| 部署 | POST | `/deployment/create/xml` | JSON 便捷通道（本项目扩展） |
| 部署 | GET | `/deployment` | 部署列表（支持分页） |
| 流程定义 | GET | `/process-definition` | 流程定义列表（支持分页） |
| 流程定义 | GET | `/process-definition/key/{key}` | 单个定义 |
| 流程定义 | GET | `/process-definition/key/{key}/xml` | XML 原文 |
| 流程实例 | POST | `/process-instance` | 启动（`definitionKey` 或 `processDefinitionId`） |
| 流程实例 | GET | `/process-instance` | 列表（支持分页，按 key/businessKey/active 过滤） |
| 流程实例 | GET | `/process-instance/{id}` | 详情 |
| 流程实例 | DELETE | `/process-instance/{id}` | 删除（历史保留，HI 置 DELETED） |
| 流程实例 | GET | `/process-instance/{id}/variables` | 全量变量 |
| 流程实例 | GET | `/process-instance/{id}/variables/{name}` | 单个变量 |
| 流程实例 | PUT | `/process-instance/{id}/variables/{name}` | 设置变量 |
| 任务 | GET | `/task` | 列表（支持分页，按 assignee/candidateUser/unassigned 过滤） |
| 任务 | GET | `/task/{id}` | 详情 |
| 任务 | POST | `/task/{id}/claim` | 认领 |
| 任务 | POST | `/task/{id}/unclaim` | 取消认领 |
| 任务 | POST | `/task/{id}/assignee` | 指派（无 userId 视为 unclaim） |
| 任务 | POST | `/task/{id}/complete` | 完成（带 variables） |
| 历史 | GET | `/history/process-instance` | 历史流程实例（支持分页） |
| 历史 | GET | `/history/task` | 历史任务（含待办视图，支持分页） |
| 历史 | GET | `/history/activity-instance` | 历史活动实例（支持分页） |
| 历史 | GET | `/history/variable-instance` | 历史变量快照（支持分页） |
| 决策 | GET | `/decision-definition` | 决策定义列表（支持分页） |
| 决策 | GET | `/decision-definition/key/{key}` | 单个决策定义 |
| 决策 | POST | `/decision-definition/key/{key}/evaluate` | 决策表求值 |

**前缀**：所有端点都挂在 `/engine-rest` 下（Camunda 7 一致）。

### 14.3 变量形态（包装 vs 裸值）

入参两种写法兼容：

```bash
# 包装形态（Camunda 官方）
curl -XPOST http://localhost:8080/engine-rest/process-instance \
     -H 'Content-Type: application/json' \
     -d '{"definitionKey":"loan-approval","variables":{"amount":{"value":20000,"type":"Long"}}}'

# 裸值形态（本项目额外支持，简化脚本）
curl -XPOST http://localhost:8080/engine-rest/process-instance \
     -H 'Content-Type: application/json' \
     -d '{"definitionKey":"loan-approval","variables":{"amount":20000}}'
```

出参默认包装形态（对齐 Camunda），加 `?bare=true` 退化为裸值 map：

```bash
curl 'http://localhost:8080/engine-rest/process-instance/abc/variables?bare=true'
# {"name":"world","amount":20000}  而不是 {"name":{"value":"world"},"amount":{"value":20000}}
```

### 14.4 分页（9 个列表端点统一）

```bash
curl 'http://localhost:8080/engine-rest/process-instance?firstResult=0&maxResults=20'
curl 'http://localhost:8080/engine-rest/task?firstResult=20&maxResults=20'
```

| 参数 | 含义 | 默认 | 上限 |
|---|---|---|---|
| `firstResult` | 偏移量（0 基） | 0 | 非法值（<0）→ 422 |
| `maxResults` | 每页条数 | 200 | 1000（>1000 自动 clamp） |

末页判定：返回数 < maxResults 即末页（响应仍是裸数组，**不包 count/total**，向后兼容 Camunda）。

### 14.5 错误响应体

```json
{"type": "ProcessEngineException", "message": "No outgoing sequence flow satisfies the condition"}
```

| 状态码 | 含义 |
|---|---|
| 404 | 实例/任务/定义不存在 |
| 400 | 部署失败 / 参数非法 |
| 409 | 实例状态冲突（如启动已完成实例） |
| 500 | 引擎内部异常 |

完整 demo 见 [`examples/run_api_demo.py`](../examples/run_api_demo.py)。

---

## 15. 教程 13：多进程 JobExecutor 部署

### 15.1 为什么需要多进程

JobExecutor 默认是单进程的——单进程崩了所有 timer / async 都停。生产至少双进程互备：

```
                   ┌─ JobExecutor-A (host=node1)
camunda store  <───┤
   (SQLite/PG)    └─ JobExecutor-B (host=node2)
```

同一时刻只能有一个 executor 持有某个 job 锁——靠 DB CAS lease 协调。

### 15.2 部署代码

```python
# worker.py
from camunda.engine import ProcessEngine
from camunda.job import JobExecutor
from camunda.persistence.store import Store

engine = ProcessEngine(store=Store(os.environ["DB_URL"]))
register_all_delegates(engine)              # 你自己的 register 函数

exec = JobExecutor(
    engine,
    name=os.environ.get("WORKER_NAME", socket.gethostname()),
    lease_seconds=300,                       # 持锁时长（防僵尸）
)
exec.start()
```

启动 2 份即可：

```bash
WORKER_NAME=worker-a DB_URL=postgresql+psycopg://.../camunda python worker.py
WORKER_NAME=worker-b DB_URL=postgresql+psycopg://.../camunda python worker.py
```

### 15.3 lock_owner 自动分配

`JobExecutor` 启动时按 `name-pid-hostname-uuid8` 自动生成唯一 `lock_owner`。同一进程重启 lock_owner 会变（pid + uuid 都换）——所以 lease 过期后别的 executor 可以接管。

手动指定（高级）：

```python
exec = JobExecutor(engine, name="worker-a", lock_owner="worker-a-shard-1")
```

### 15.4 lease_seconds 怎么选

| 场景 | 建议 |
|---|---|
| 快速 async 任务（< 30s） | 60s |
| 中等耗时（30s~5min） | 300s |
| 长作业（> 5min） | 1800s + delegate 内调 `store.extend_lock()` 续约 |

### 15.5 续约

长作业执行期间调 `store.extend_lock(job_id, owner, lease_seconds, now)` 把 lease 延后：

```python
import threading

def long_running_delegate(variables):
    job_id = variables["_current_job_id"]      # 引擎自动注入
    def heartbeat():
        store.extend_lock(job_id, owner, 300, datetime.now())
        threading.Timer(60, heartbeat).start()
    threading.Timer(60, heartbeat).start()
    do_real_work()
```

CAS 失败 = 锁已被别人接管，当前执行应中止提交。

### 15.6 监控

```python
store.list_locks()                            # 所有持锁作业
store.list_locks(lock_owner="worker-a")       # 仅看 worker-a
```

返回示例：

```python
[{"id": "...", "lock_owner": "worker-a-1234-host01-a1b2c3d4",
  "lock_exp_time": "2026-09-05T20:00:00+08:00",
  "type": "timer-transition", "retries": 3}]
```

### 15.7 故障场景演练

| 场景 | 现象 | 引擎行为 |
|---|---|---|
| Worker A 持锁中崩溃 | lease 不主动归还 | lease 到期后 B 可重新 CAS 抢到 |
| Worker A 持锁但 hang（无响应） | lease 未到期 | B 等过期；A 心跳续约的话 B 抢不到 |
| Worker A 完成提交时已过期 | CAS 失败（affected_rows=0） | 引擎检测到「锁已变」，跳过提交（避免双写） |

完整 demo 见 [`examples/run_lock_demo.py`](../examples/run_lock_demo.py)（双 owner 5 轮同步 tick + lease 过期接管）。

---

## 16. 常见坑 FAQ

### 16.1 「卡住不前进」

90% 是 **delegate 没注册**。诊断：

```python
print(engine._delegates)   # 查看已注册
```

也可能是表达式求值失败（变量不存在返回 `null`，但比较错就抛）。看堆栈第一行。

### 16.2 「实例跑完了变量没出来」

delegate 签名错误：

```python
# ❌ 引擎不知道你返回了啥
def bad_delegate(variables):
    return {"greeting": "hi"}      # 返回值被丢弃

# ✅ 就地修改
def good_delegate(variables):
    variables["greeting"] = "hi"
```

### 16.3 「持久化版本重启后任务卡死」

DB 不存 delegate。**必须每次启动重新 `register_delegate()`**（详见 §8.4）。

### 16.4 「deploy 报错 'duplicate id'」

同一 `<process>` 内 id 重复。检查：节点 id、连线 id、子流程嵌套的内部 id。

### 16.5 「跨容器连线」

子流程 A 内部节点 → 子流程 B 内部节点：❌ 不支持。引擎启动时报错「cross-container sequence flow」。

正确：连线只能在同层；跨层通过父层节点中转。

### 16.6 「多实例 collection 为空」

`${items}` 变量在启动时未传入 / 传入 `null` / 传入不是 list 类型。

```python
pi = engine.start_process_instance_by_key("flow", {"items": []})   # 空集合：直接跳过
pi = engine.start_process_instance_by_key("flow", {"items": ["a", "b"]})   # OK
```

### 16.7 「JobExecutor 跑不动 timer」

检查：

1. `JobExecutor.start()` 调用了？（不是 `start = JobExecutor(...)`）
2. `poll_interval` 不是 0？（0 = 死循环）
3. `engine` 持有 `Store`？（无 Store 时 JobExecutor 走内存路径，DB 路径需要 Store）

### 16.8 「Lease 频繁过期 = 工作经常重复执行」

`lease_seconds` 太短或 delegate 太久。`store.list_locks()` 看 `lock_exp_time`，调大 lease 或加 `extend_lock` 心跳。

### 16.9 「信号 throw 了但没人接」

- 信号名拼错（`throw_signal("maintenance")` 与 `<signal name="maintenance"/>` 不一致）
- 订阅是动态的——throw 时还没人订阅 → 静默丢弃（**这是预期**，不是 bug）
- signal 必须是 `<definitions>` 下显式声明（不是 message 那种 messageRef）

### 16.10 「DMN 求值报错 'unknown variable'」

DMN 的 `inputExpression` 引用变量名不在入参 dict 里：

```python
dmn.evaluate_decision("loan_grading", {"amount": 9000})   # 缺 credit_score → None
```

输入变量名必须与 DMN 表格里的 `<text>credit_score</text>` 完全一致。

### 16.11 「边界事件没触发」

- 边界事件是否真的挂上了宿主（`attachedToRef` 写对了？）
- 边界事件 trigger 类型跟宿主节点匹配（timer boundary 不能挂 start event）
- 时间表达式合法？`PT5S` 写成 `5S` → 解析失败 → 启动时报错

### 16.12 「并行网关永远在等」

至少一条入边的 token 没到。`engine.create_execution_query()` 查哪些 execution 还在等 join。

### 16.13 「async 后没动」

JobExecutor 没启动，或 lease 已过期被别人抢走。看 `store.list_locks()`。

### 16.14 「FastAPI body 参数被吞掉」

如果你定义了 `Optional[X]` 但没 import `Optional`，`from __future__ import annotations` 会让 FastAPI 静默丢掉这个 body 参数，**不报错**。

修法：补 import `from typing import Optional`（或直接用 `X | None = None`，Python 3.10+）。本项目 M6 实现时踩过这个坑——`Optional` 没 import 时 FastAPI 把整个 body 参数「降级」，导致 422/400 都不是，只是默认值生效。

### 16.15 「engine.start_process_instance_by_key 报 NoSuchProcessDefinition」

- `definitionKey` 拼写错（区分大小写）
- 还没 deploy
- deploy 用了错误的 BPMN 命名空间导致 process 没被识别

`engine.list_process_definitions()` 看实际部署了什么。

---

## 17. 调试技巧

### 17.1 历史查询定位卡点

```python
# 实例级别
pi = engine.get_process_instance(instance_id)
print("activity_history:", pi.activity_history)   # 所有经过的节点
print("active_tasks:", engine.create_task_query(process_instance_id=instance_id))
print("variables:", pi.variables)
print("is_completed:", pi.is_completed)
```

并行网关「卡 join」的场景——`activity_history` 末尾能看到只走了一半的路径，对照 BPMN 看哪条 token 没到。

### 17.2 JobExecutor 锁状态

```python
store.list_locks()                  # 当前所有持锁作业
store.list_locks(lock_owner="...")  # 单 worker
```

### 17.3 SQL 直接查询

```bash
sqlite3 camunda.db <<EOF
.headers on
.mode column
SELECT ID_, PROC_DEF_KEY_, STATE_ FROM ACT_RU_EXECUTION;
SELECT ID_, NAME_, ASSIGNEE_ FROM ACT_RU_TASK;
SELECT ID_, TYPE_, LOCK_OWNER_, LOCK_EXP_TIME_, DUEDATE_, RETRIES_ FROM ACT_RU_JOB;
EOF
```

### 17.4 表达式求值试错

内部 API 是 `camunda.engine.expression.evaluate_condition`，但**不建议直接调**——把它当 E2E 行为验证。如果非要试：

```python
from camunda.engine.expression import evaluate_condition
evaluate_condition("${amount > 1000}", {"amount": 2000})   # → True
```

### 17.5 mock 时间（冻结 / 拨快）

引擎与 JobExecutor 通过 `camunda.common.clock` 全局取时间。测试里覆盖它：

```python
from datetime import datetime, timedelta
from camunda.common.clock import set_clock, reset_clock

def fake_now() -> str:
    return (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")

set_clock(fake_now)
try:
    ex.tick()            # 看到的时间是 fake_now 的返回值
finally:
    reset_clock()        # 测试结束务必还原，否则污染后续
```

完整示例见 `tests/unit/test_timer*.py`（多处使用）。

### 17.6 启用日志

```python
import logging
logging.basicConfig(level=logging.DEBUG)
# camunda.* 各模块都有 logger，按需开
```

### 17.7 REST API 调试

直接看 Swagger UI（`/docs`）——所有端点的请求/响应 schema 都在那。

---

## 18. 生产部署清单

### 18.1 启动前必做

- [ ] **DB 切换到 PostgreSQL**（生产强烈不建议 SQLite，单写并发 + 无网络访问）
- [ ] **delegate 注册集中化**（统一 `register_all(engine)`，启动时调用一次）
- [ ] **历史级别配置**（默认全量写 HI 表，长期跑会膨胀；建议加定时归档/清理任务——本项目暂无内置，自己加）
- [ ] **REST 鉴权**（本项目**没做**——FastAPI app 直接暴露。生产前面套 nginx/oauth2-proxy，或 fork 一个加 Depends(get_user) 的中间件）
- [ ] **DMN 重部署**（DB 不存 DMN，启动时从代码仓库 `deploy_dmn()`，否则 businessRuleTask 跑不起来）

### 18.2 进程模型

推荐：**双 worker + 单 API**：

```
┌─────────────────┐
│  API (uvicorn)  │  ← REST 端点，无 JobExecutor
└─────────────────┘
         │
         ▼
┌─────────────────────────────────────┐
│  DB (PostgreSQL: ACT_RE/RU/HI)      │
└─────────────────────────────────────┘
         ▲                ▲
         │                │
┌────────┴────┐   ┌──────┴────────┐
│ Worker A    │   │ Worker B       │
│ JobExecutor │   │ JobExecutor    │   ← 多副本抢锁
└─────────────┘   └────────────────┘
```

API 进程无状态（可横向扩），Worker 数 ≥ 2 互备。

### 18.3 监控

最小监控项：

| 指标 | 来源 | 告警阈值 |
|---|---|---|
| 持锁作业数（按 owner） | `store.list_locks()` | 长期持锁 > 10 分钟 |
| due job 积压 | `len(store.list_due_jobs(limit=9999))` | > 100 |
| 死信作业数 | `len(store.list_jobs(retries=0))` | > 0 |
| 活跃实例数 | `engine.list_process_instances(active=True)` | 业务相关 |

### 18.4 已知不做的事

| 功能 | 原因 | 替代方案 |
|---|---|---|
| REST 鉴权 | 不归引擎核心 | nginx / oauth2-proxy 前置 |
| Web 控制台 | 项目定位 | 自己写（已有 REST） |
| 流程定义热更新 | 启动时一次性 deploy | 重启 worker |
| 历史表自动归档 | 无内置 | 定时 SQL 任务 |
| DMN 定义持久化 | 与 delegate 同源 | 启动时 deploy_dmn |
| Camunda Operate / Cockpit | 是 Camunda 商业产品 | 不在范围 |
| Zeebe 协议兼容 | 项目对齐 Camunda 7 不是 Camunda 8 | 不要混用 |

### 18.5 升级到新版本

1. 看 [CHANGELOG.md](../CHANGELOG.md)（暂无，独立版本自己加）
2. 跑 `pytest` 确认无破坏
3. 测试环境跑一遍关键流程（含持久化恢复）
4. 灰度切流量

---

## 附录 A：完整端点列表（27 个 REST 路由）

| 路径前缀 | 路由模块 | 端点数 |
|---|---|---|
| `/engine-rest/deployment*` | `camunda/api/routers/deployment.py` | 3 |
| `/engine-rest/process-definition*` | `camunda/api/routers/process_definition.py` | 3 |
| `/engine-rest/process-instance*` | `camunda/api/routers/process_instance.py` | 7 |
| `/engine-rest/task*` | `camunda/api/routers/task.py` | 6 |
| `/engine-rest/history/*` | `camunda/api/routers/history.py` | 4 |
| `/engine-rest/decision-definition*` | `camunda/api/routers/decision.py` | 3 |

## 附录 B：测试统计

- **268 个单测**（M1 流转 + M2 持久化 + M3 作业 + M4-1 边界/asyncAfter + M4-2a 子流程 + M4-2b 事件子流程/非中断式边界 + M4-2c 多实例 + M4-2d 消息/信号事件 + 持久化恢复 + M5 DMN + M6 REST + M7 store CAS lease + 多 executor 防双执行 + M8 REST 列表分页）
- **10 个 demo**（`run_demo / timer / boundary / subprocess / ni / mi / msg_sig / dmn / api / lock`）
- 跑通 1.87s

## 附录 C：参考资源

- Camunda 7 官方文档：<https://docs.camunda.org/manual/7.23/>
- BPMN 2.0 规范：<https://www.omg.org/spec/BPMN/2.0/>
- DMN 1.3 规范：<https://www.omg.org/spec/DMN/1.3/>
- FEEL 规范（子集）：<https://www.omg.org/spec/DMN/1.3/#feel-semantics>