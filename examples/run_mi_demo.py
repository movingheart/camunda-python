"""M4-2c 多实例（MI）三种宿主形态演示。

用法：
    python examples/run_mi_demo.py

演示内容（连锁门店「新品铺货」多实例场景，全部由脚本驱动完成、无真实等待）：
    1. serviceTask 宿主（并行，batch-notify）：向 3 个区域同步推送铺货通知 ——
       delegate 同步执行无等待窗口，start 返回即全部实例完成并收束；
       打印 loopCounter/元素变量按序注入，收尾变量清理。
    2. subProcess 宿主（顺序，store-seq）：3 家门店逐家执行铺货子流程 ——
       同一时刻仅 1 家店在子流程内部停等「店长人工确认」，完成一家才续跑
       下一家；loopCounter 递增、元素变量前移。
    3. subProcess 宿主（并行 + completionCondition，store-par）：3 家店同时进入
       子流程内部停等；任 2 家完成即满足条件 -> 剩余门店实例整树终止（内部
       人工任务取消归档）-> 容器收束、流程完成。

说明：多实例宿主（userTask/serviceTask/subProcess）为纯 MI 范围，无 timer/
async 拆分，因此本 demo 不需要 JobExecutor，直接同步驱动人工任务完成即可；
脚本末尾断言全部通过。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camunda.engine import ProcessEngine
from camunda.parser import parse_bpmn_file

EXAMPLES = Path(__file__).resolve().parent


def new_engine() -> ProcessEngine:
    engine = ProcessEngine()

    def notify_region(vars_):
        region = vars_.get("region", "?")
        loop = vars_.get("loopCounter", "?")
        print(f"    [notifyRegion] #{loop} 区域「{region}」已推送铺货通知")

    engine.register_delegate("notifyRegion", notify_region)
    engine.deploy(parse_bpmn_file(str(EXAMPLES / "mi-rollout.bpmn")))
    return engine


def tree_lines(pi, e=None, depth: int = 0) -> list[str]:
    """execution 树逐行描述：role/activity/mi 状态。"""
    if e is None:
        e = pi.root_execution
    pad = "  " * depth
    mi = ""
    if e.mi:
        seq = "seq" if e.mi.get("sequential") else "par"
        if "index" in e.mi:
            mi = f" mi[{seq} 实例 idx={e.mi['index']}]"
        else:
            mi = (f" mi[{seq} 容器 total={e.mi.get('total')} "
                  f"done={e.mi.get('completed')} active={e.mi.get('active')}]")
    lines = [f"{pad}- {e.id[:8]} role={e.role} act={e.activity_id or '-'}{mi}"]
    for c in e.children:
        lines.extend(tree_lines(pi, c, depth + 1))
    return lines


def show_tree(pi, title: str) -> None:
    print(f"    execution 树（{title}）:")
    for line in tree_lines(pi):
        print(f"      {line}")


def demo_sync_service_host() -> None:
    """演示 1：serviceTask 宿主（并行）—— 同步 delegate，start 返回即收束。"""
    print("=" * 66)
    print("演示 1：serviceTask 宿主（batch-notify，3 区域同步推送）")
    print("=" * 66)
    engine = new_engine()
    pi = engine.start_process_instance_by_key(
        "batch-notify", {"regions": ["华东", "华南", "华北"]}
    )
    # 同步宿主：无等待窗口，start 返回即已全部执行完并收束
    assert engine.get_process_instance(pi.id).is_completed
    assert pi.root_execution.mi is None and pi.root_execution.role == "TOKEN"
    assert engine.create_task_query() == []
    # 每实例一条 serviceTask actinst（已结算），容器无独立痕迹
    acts = [a for a in pi.activity_history if a.activity_id == "batchNotify"]
    assert len(acts) == 3 and all(a.end_time is not None for a in acts)
    print("    start 返回即 3 区域同步推送完毕（delegate 在引擎内逐个执行）")
    print(f"    actinst：batchNotify × {len(acts)}（全部结算）")
    assert "region" not in pi.variables and "loopCounter" not in pi.variables
    print("    行为期注入的 loopCounter/elementVariable 已随容器收尾清理")


def demo_sequential_subprocess() -> None:
    """演示 2：subProcess 宿主（顺序）—— 一次仅 1 家店在内部，完成续跑下一家。"""
    print("=" * 66)
    print("演示 2：subProcess 宿主（store-seq，3 家店逐家铺货）")
    print("=" * 66)
    engine = new_engine()
    stores = ["杭州店", "南京店", "苏州店"]
    pi = engine.start_process_instance_by_key("store-seq", {"stores": stores})
    root = pi.root_execution
    # 容器 token 兼实例载体：已进子流程（SCOPE@storeSeq），仅 1 家店在内部
    assert root.is_mi_container and root.mi["sequential"] is True
    assert root.mi["total"] == 3 and root.role == "SCOPE"
    assert root.activity_id == "storeSeq" and len(root.children) == 1
    assert pi.variables["store"] == "杭州店"  # 当前实例元素变量可见
    show_tree(pi, "启动后：仅 1 实例在子流程内部停等")
    for expect in range(3):
        (task,) = engine.create_task_query(process_instance_id=pi.id)
        assert task.task_definition_key == "storeApproveS"
        print(
            f"    第 {expect + 1} 家店「{pi.variables['store']}」待店长确认"
            f"（loopCounter={pi.variables['loopCounter']}）"
        )
        engine.complete_task(task.id)
        if expect < 2:
            assert root.is_mi_container and root.mi["completed"] == expect + 1
            assert len(engine.create_task_query(process_instance_id=pi.id)) == 1
            assert pi.variables["store"] == stores[expect + 1]  # 元素变量前移
            print(f"    -> 确认完成，续跑下一家（completed={expect + 1}/3）")
        else:
            assert engine.get_process_instance(pi.id).is_completed
            assert root.mi is None and root.role == "TOKEN"
            print("    -> 第 3 家确认完成，实例收束、流程结束")
    assert engine.create_task_query() == []
    # actinst：subProcess 宿主 3 条结算 + 内部 userTask 3 条结算
    batch_acts = [a for a in pi.activity_history if a.activity_id == "storeSeq"]
    assert len(batch_acts) == 3 and all(a.end_time is not None for a in batch_acts)
    inner_acts = [a for a in pi.activity_history if a.activity_id == "storeApproveS"]
    assert len(inner_acts) == 3 and all(a.end_time is not None for a in inner_acts)
    assert "store" not in pi.variables and "loopCounter" not in pi.variables
    print(f"    actinst：storeSeq × 3 + storeApproveS × 3（全部结算，变量已清理）")


def demo_parallel_subprocess_early_stop() -> None:
    """演示 3：subProcess 宿主（并行 + completionCondition）—— 2 家完成即收束。"""
    print("=" * 66)
    print("演示 3：subProcess 宿主（store-par，3 家并行 + 任 2 家完成即收束）")
    print("=" * 66)
    engine = new_engine()
    stores = ["北京店", "上海店", "广州店"]
    pi = engine.start_process_instance_by_key("store-par", {"stores": stores})
    root = pi.root_execution
    # 3 实例并行展开：容器 SCOPE@storePar + 3 child 各自进入内部停等
    assert root.role == "SCOPE" and root.activity_id == "storePar"
    assert root.is_mi_container and root.mi["total"] == 3 and root.mi["active"] == 3
    assert len(root.children) == 3
    assert len(engine.create_task_query(process_instance_id=pi.id)) == 3
    show_tree(pi, "启动后：3 实例并行、各自停内部人工确认")
    # 完成两家 -> 条件满足，第 3 家整树终止
    for i in range(2):
        (task,) = engine.create_task_query(process_instance_id=pi.id)[:1]
        print(f"    「{task.name or pi.variables.get('store')}」待确认 -> 完成")
        engine.complete_task(task.id)
        if i == 0:
            assert not engine.get_process_instance(pi.id).is_completed
            assert root.mi["completed"] == 1 and root.mi["active"] == 2
    # 条件 nrOfCompletedInstances >= 2 满足：第 3 家（含内部任务）被整树终止
    assert engine.get_process_instance(pi.id).is_completed
    assert root.mi is None and root.role == "TOKEN"
    assert engine.create_task_query() == []  # 无残留任务
    print("    第 2 家完成 -> completionCondition 满足 -> 第 3 家实例被终止")
    show_tree(pi, "收束后：容器沿出边离开、流程完成")
    # 3 条内部任务全部归档（2 完成 + 1 被终止），均带 end_time
    archived = [t for t in pi.completed_tasks if t.task_definition_key == "storeApproveP"]
    assert len(archived) == 3 and all(t.end_time is not None for t in archived)
    batch_acts = [a for a in pi.activity_history if a.activity_id == "storePar"]
    assert len(batch_acts) == 3 and all(a.end_time is not None for a in batch_acts)
    inner_acts = [a for a in pi.activity_history if a.activity_id == "storeApproveP"]
    assert len(inner_acts) == 3 and all(a.end_time is not None for a in inner_acts)
    print(f"    3 条内部任务全归档（2 完成 + 1 终止）；storePar/storeApproveP "
          f"actinst × 3 全部结算")


if __name__ == "__main__":
    t0 = time.time()
    demo_sync_service_host()
    print()
    demo_sequential_subprocess()
    print()
    demo_parallel_subprocess_early_stop()
    print()
    print("=" * 66)
    print(f"全部演示通过 ✅  （总耗时 {time.time() - t0:.2f} 秒，纯同步驱动无等待）")
    print("=" * 66)
