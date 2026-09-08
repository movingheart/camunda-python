# camunda-python

一个面向 Python 3 的开源 BPMN / DMN 流程引擎。

提供 BPMN 2.0 流程模型解析、流转执行、DMN 决策表求值、SQLAlchemy 持久化、多实例
作业执行与可选的 REST API。适合需要把工作流嵌入 Python 服务、又不想拉一整套 Java
生态的团队。

## 特性一览

- **BPMN 2.0 流程建模**：基于 `lxml` 的自研解析器，支持用户任务 / 服务任务 / 排他与
  并行网关 / 内嵌子流程 / 多实例 / 边界与中断 / 消息与信号事件。
- **DMN 决策引擎**：独立可用，支持六种 `hitPolicy`（`UNIQUE` / `FIRST` / `ANY` /
  `PRIORITY` / `RULE ORDER` / `COLLECT[+SUM/MIN/MAX/COUNT]`）与 FEEL 表达式子集
  （数值算术 / 比较 / 区间 / 列表 / `not(...)` / `null`），并可通过 BPMN
  `businessRuleTask` 集成进流程。
- **持久化**：SQLAlchemy 2.0，兼容 **SQLite / PostgreSQL / MySQL**。崩溃后
  `ProcessEngine.from_database()` 一行恢复。
- **作业执行器**：Timer Start / Timer Catch / `asyncBefore / asyncAfter` / 失败重试；
  `JobExecutor` 后台线程自动推进。
- **多节点抢锁**：跨进程 / 多 JobExecutor 通过 DB CAS lease 保证同一作业不被重复执行，
  长作业支持续约。
- **REST API**：FastAPI 兼容 Camunda 7 `engine-rest` 常用端点（部署 / 流程定义 /
  流程实例 / 任务 / 历史 / 决策），9 个列表端点支持 `firstResult` + `maxResults` 分页。

## 快速开始

```bash
pip install camunda-python             # 核心（BPMN / DMN / 持久化 / JobExecutor）
pip install camunda-python[api]        # 可选：REST API（fastapi + uvicorn）
```

最小可运行示例（5 行跑通一个贷款审批流程）：

```python
from pathlib import Path
from camunda.parser import parse_bpmn_xml
from camunda.engine import ProcessEngine

xml = Path("examples/loan-approval.bpmn").read_text()
engine = ProcessEngine()
engine.deploy(parse_bpmn_xml(xml, source_name="loan-approval.bpmn"))

pi = engine.start_process_instance_by_key("loan-approval", {"amount": 20000})
task = engine.create_task_query(process_instance_id=pi.id)[0]
engine.complete_task(task.id, {"approved": True})
```

启动 REST 服务：

```bash
uvicorn camunda.api.app:create_app --factory --port 8080
# 交互式 API 文档：http://127.0.0.1:8080/docs
```

## 文档导航

| 想做什么 | 看哪里 |
|---|---|
| 第一次接触，从「5 分钟 hello world」走到「生产部署」 | **[docs/USER_GUIDE.md](docs/USER_GUIDE.md)** |
| 看引擎设计 / 模块划分 / 与 Camunda 7 的差异 | **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** |
| 看每个示例跑什么场景 | **[examples/](examples/)** |

## 示例

仓库自带可直接 `python examples/xxx.py` 运行的可执行示例：

| 示例 | 演示的引擎能力 |
|---|---|
| `run_demo.py` | 排他网关 + 用户任务 + 服务任务（贷款审批） |
| `run_timer_demo.py` | Timer Start / Timer Catch / `asyncBefore`（真实时钟） |
| `run_boundary_demo.py` | Timer 边界事件中断 + `asyncAfter`（审批超时降级） |
| `run_subprocess_demo.py` | 内嵌子流程 + 子流程级边界 timer（订单履约 / 退款） |
| `run_ni_demo.py` | 非中断式边界（`cancelActivity=false` + 并发收束） |
| `run_mi_demo.py` | 多实例三宿主（userTask / serviceTask / subProcess） |
| `run_msg_sig_demo.py` | 消息 1:1 关联 / 信号广播 / 订阅恢复 |
| `run_dmn_demo.py` | DMN 直接求值 / `businessRuleTask` 集成 / 决策驱动网关 |
| `run_api_demo.py` | 真实起 uvicorn，部署 / 启动 / 任务 / 历史 / 决策端到端 |
| `run_lock_demo.py` | 双 JobExecutor 共享 Store，验证 CAS lease 抢锁 |

## 测试与开发

```bash
git clone https://github.com/movingheart/camunda-python.git
cd camunda-python
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,api]"

pytest                       # 跑测试套件
pytest -k test_dmn            # 只跑某个子模块
pytest --cov=camunda          # 带覆盖率
```

CI 在 GitHub Actions 上跑 Python 3.12 / 3.13，每次 push 触发。

## 与 Camunda 7 的差异

设计目标是**语义对齐**——核心流程元素、状态机、持久化表结构与 Camunda 7 保持一致，
方便已有 Camunda 7 经验的人快速理解。但以下几点**有意取舍**：

- **不做**：Cockpit / Tasklist Web 控制台、身份与权限、外部任务（external task）。
- **轻量差异**：FEEL 仅支持子集（无函数调用 / 日期时间 / 路径表达式）；
  DMN 仅支持 `decisionTable` 形态；DMN 部署不落库（崩溃恢复后需重新 `deploy_dmn`）。
- **多进程抢锁**：用应用层 CAS（`UPDATE ... WHERE LOCK_EXP_TIME_ < now`）替代
  `SELECT FOR UPDATE`，跨 SQLite / PostgreSQL / MySQL 三个方言一致。
- **持久化策略**：命令边界 delete + insert 全量重写（与 Camunda 7 逐行 update 不同），
  对中小流程足够快，恢复语义更直白。

完整差异表见 [docs/ARCHITECTURE.md §设计取舍](docs/ARCHITECTURE.md)。

## 许可

Apache-2.0。详见 [LICENSE](LICENSE)。

变更记录见 [CHANGELOG.md](CHANGELOG.md)。