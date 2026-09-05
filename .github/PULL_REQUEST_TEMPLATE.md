## 描述

<!-- 一句话说清楚改了什么 -->

## 关联 Issue

<!-- 用 `Closes #123` / `Refs #456` 关联 -->

- Closes #
- Refs #

## 改动类型

<!-- 勾选主要类别；可多选 -->

- [ ] Bug fix（向后兼容）
- [ ] New feature（向后兼容）
- [ ] Breaking change（不兼容旧行为，需要在描述里写迁移指南）
- [ ] Documentation only
- [ ] Refactor / 内部结构调整（对外行为不变）
- [ ] Test only
- [ ] CI / 构建 / 工具链

## 涉及模块

<!-- 提示 reviewer 看哪里 -->

- [ ] BPMN 引擎核心（`camunda/engine/`、`camunda/model/`、`camunda/parser/`）
- [ ] DMN（`camunda/dmn/`）
- [ ] 持久化 / store（`camunda/persistence/`）
- [ ] JobExecutor（`camunda/job/`）
- [ ] REST API（`camunda/api/`）
- [ ] 模型 / 解析（`camunda/parser/`）
- [ ] 文档（`README.md` / `docs/`）
- [ ] 测试（`tests/`）
- [ ] Demo（`examples/`）

## 验证清单

- [ ] 我在本地跑了 `pytest tests/`，全过
- [ ] 我跑了相关的 `examples/run_*.py` 确认 demo 还能跑
- [ ] 我加了 / 改了对应的单测，**新单测本地全过**
- [ ] 我改了 `docs/ARCHITECTURE.md` 对应章节（如果动了设计 / 与 Camunda 7 的差异）
- [ ] 我改了 `docs/USER_GUIDE.md` 对应章节（如果用户可见行为变了）
- [ ] 我改了 `README.md` 里程碑表（如果是 milestone 级别的改动）

## 截图 / 日志（如果适用）

<!-- 多放现象，少放废话。截图只截关键区域 -->

## Checklist

- [ ] 我跑过 `git diff main --stat` 确认没有意外改动
- [ ] commit message 描述了「为什么改」而不只是「改了什么」
- [ ] 没有遗留 `print(...)` / `breakpoint()` / 调试日志
- [ ] 我没改别人 PR 的 commit（避免 rebase 出乱）
