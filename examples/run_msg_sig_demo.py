"""M4-2d 消息/信号事件演示（correlate_message 1:1 关联 + throw_signal 广播 + 崩溃恢复）。

用法：
    python examples/run_msg_sig_demo.py

演示内容：
    1. 跨实例消息接力：两笔订单同时等待支付回调，correlate_message 按 1:1
       精准投递注册序最早的实例（变量合并），逐一关联完成发货
    2. 信号广播通知：多个工单实例并行处理中，throw_signal 跨实例广播维护
       事件——每个实例的非中断式信号边界各自 spawn 通知并发线，主线不受影响
    3. 崩溃恢复：实例停在消息 catch 停等落库 -> from_database 重启 ->
       订阅重推导 -> correlate 续跑完成

说明：订阅为纯内存派生态（不落库），恢复依赖 execution 树重推导；
脚本末尾断言全部通过。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from camunda.engine import ProcessEngine
from camunda.parser import parse_bpmn_file
from camunda.persistence.store import Store

EXAMPLES = Path(__file__).resolve().parent


def demo_message_relay() -> None:
    """演示 1：跨实例消息接力（1:1 关联 + 注册序消歧 + 变量合并）。"""
    print("=" * 66)
    print("演示 1：消息接力（两笔订单等支付回调，correlate 逐笔精准投递）")
    print("=" * 66)
    engine = ProcessEngine()
    shipped: list[str] = []

    def ship(vars_):
        shipped.append(vars_["order"])
        print(f"    [ship] 订单 {vars_['order']} 已发货")

    engine.register_delegate("ship", ship)
    engine.deploy(parse_bpmn_file(str(EXAMPLES / "msg-sig-relay.bpmn")))

    order_a = engine.start_process_instance_by_key("order-relay", variables={"order": "A-001"})
    order_b = engine.start_process_instance_by_key("order-relay", variables={"order": "B-002"})
    print(f"两笔订单已下单，均在支付回调停等：A-001={order_a.id[:8]} B-002={order_b.id[:8]}")
    print(f"等待中的消息订阅数：{len(engine._event_subs)}")

    # 回调 1：未指定实例 -> 1:1 投递注册序最早（A-001），变量随消息合并
    engine.correlate_message("paymentReceived", variables={"paid": True})
    assert engine.get_process_instance(order_a.id).is_completed
    assert not engine.get_process_instance(order_b.id).is_completed
    print("回调1 到达 -> 精准投递最早注册的 A-001 -> 发货完成")

    # 回调 2：轮到 B-002（此时仅剩它一个订阅）
    engine.correlate_message("paymentReceived")
    assert engine.get_process_instance(order_b.id).is_completed
    print("回调2 到达 -> 投递 B-002 -> 发货完成")
    assert shipped == ["A-001", "B-002"]
    print()
    return engine


def demo_signal_broadcast() -> None:
    """演示 2：信号广播（throw_signal 跨实例 + 非中断边界 spawn 并发线）。"""
    print("=" * 66)
    print("演示 2：信号广播（3 个工单实例并行，维护事件一键广播全部命中）")
    print("=" * 66)
    engine = ProcessEngine()
    engine.deploy(parse_bpmn_file(str(EXAMPLES / "msg-sig-broadcast.bpmn")))

    pis = [engine.start_process_instance_by_key("system-notice") for _ in range(3)]
    print(f"3 个工单实例并行处理中：{[pi.id[:8] for pi in pis]}")
    for pi in pis:
        assert len(engine.create_task_query(process_instance_id=pi.id)) == 1

    # 运维发起维护广播：一次性命中全部 3 个实例的非中断信号边界
    hits = engine.throw_signal("maintenance", variables={"window": "02:00-03:00"})
    assert hits == 3
    print(f"广播 maintenance -> 命中 {hits} 个实例，各自 spawn 通知并发线")
    for pi in pis:
        got = engine.get_process_instance(pi.id)
        assert got.variables["window"] == "02:00-03:00"
        # 非中断：主线工单任务保留；广播常驻可再次触发
        assert len(engine.create_task_query(process_instance_id=pi.id)) == 1
    assert engine.throw_signal("maintenance") == 3
    print("再次广播仍命中 3 个（非中断订阅常驻可重复触发）")

    # 工单逐一处理完 -> 主线收束 -> 实例完成 -> 订阅随宿主撤销
    for pi in pis:
        (task,) = engine.create_task_query(process_instance_id=pi.id)
        engine.complete_task(task.id)
        assert engine.get_process_instance(pi.id).is_completed
    assert engine._event_subs == {}
    print("工单处理完毕 -> 3 个实例全部完成，边界订阅随宿主撤销")
    print()


def demo_crash_recovery() -> None:
    """演示 3：崩溃恢复 —— 消息 catch 停等落库，重启后订阅重推导续跑。"""
    print("=" * 66)
    print("演示 3：崩溃恢复（支付停等落库 -> 重启 -> 订阅重推导 -> 关联续跑）")
    print("=" * 66)
    db = str(EXAMPLES / "msg-sig-demo.db")
    if Path(db).exists():
        Path(db).unlink()

    engine1 = ProcessEngine(store=Store(db))
    engine1.register_delegate("ship", lambda v: v.update(shipped=True))
    engine1.deploy(parse_bpmn_file(str(EXAMPLES / "msg-sig-relay.bpmn")))
    pi1 = engine1.start_process_instance_by_key("order-relay", variables={"order": "C-003"})
    print(f"订单 C-003 已在支付回调停等并落库：{pi1.id[:8]}")
    print("    >>> 模拟进程崩溃：引擎直接丢弃（订阅不落库） <<<")

    # 重启：订阅为纯内存派生态，from_database 从 execution 树重推导
    engine2 = ProcessEngine.from_database(db)
    engine2.register_delegate("ship", lambda v: v.update(shipped=True))
    (pi2,) = engine2.list_process_instances()
    assert pi2.id == pi1.id
    print(f"重启恢复：实例 {pi2.id[:8]} 位置={pi2.root_execution.activity_id}")
    assert len(engine2._event_subs) == 1, "恢复重推导应还原 1 条 catch 订阅"
    print("    消息订阅已从 execution 树重推导（无需重新部署/重新等待）")

    engine2.correlate_message("paymentReceived", variables={"paid": True})
    assert engine2.get_process_instance(pi1.id).is_completed
    assert Store(db).load_active_instances() == [], "RU 应随实例完成清空"
    print("重启后回调到达 -> 发货续跑 -> 实例完成，RU 清空")
    Path(db).unlink()  # 清理演示库
    print()


if __name__ == "__main__":
    demo_message_relay()
    demo_signal_broadcast()
    demo_crash_recovery()
    print("=" * 66)
    print("全部演示通过 ✅")
    print("=" * 66)
