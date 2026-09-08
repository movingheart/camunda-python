"""MySQL 方言兼容性验证：>64KB BPMN XML 全链路（部署 / 读回 / 崩溃恢复）。

背景：MySQL TEXT 上限 64KB（65535 字节）。此前在 MySQL 上部署较大的 BPMN
（XML 超过 64KB）会报 ``Data too long for column 'RESOURCE_XML_'``。修复后
``entities.BIG_TEXT`` 把大文本列在 MySQL 方言下声明为 MEDIUMTEXT（16MB），
SQLite / PostgreSQL 的 Text 无长度限制不受影响。

本脚本验证全链路（可选 --local 在 SQLite 上冒烟，不连 MySQL）：

1. 连接 MySQL 并重建干净测试库（DROP + CREATE DATABASE；create_all 不改旧表，
   因此必须在全新库上验证新列类型）
2. 部署超 64KB 的 BPMN XML（构造自 examples/loan-approval.bpmn，在 <process>
   内注入约 90KB <bpmn:documentation>，不改变执行语义）
3. 读回确认 XML 完整一致（长度 + 内容逐字节比对）
4. 走完一轮启动（amount < 10000 自动通过 -> COMPLETED，顺带覆盖 RU/HI 写库）
5. ``ProcessEngine.from_database`` 重解析恢复并二次启动
6. information_schema 打印大文本列实际列类型（应含 ``mediumtext``）

用法：
    .venv\\Scripts\\python.exe scripts/verify_mysql_compat.py [--pw 密码] [--host 127.0.0.1]
    .venv\\Scripts\\python.exe scripts/verify_mysql_compat.py --local   # SQLite 冒烟

MySQL 凭据：--pw 或环境变量 MYSQL_PW 指定；缺省按常见本地 root 密码顺序尝试。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

FILL_BYTES = 90 * 1024  # 远大于 MySQL TEXT 64KB 上限，落在 MEDIUMTEXT 区间
MYSQL_TEXT_LIMIT = 65535


def build_big_xml() -> str:
    """超 64KB 的合法 BPMN：注入 <bpmn:documentation>（不参与执行语义）。"""
    base = (ROOT / "examples" / "loan-approval.bpmn").read_text(encoding="utf-8")
    marker = 'isExecutable="true">'
    assert marker in base, "BPMN 模板结构不符，无法注入"
    doc = "<bpmn:documentation>" + "x" * FILL_BYTES + "</bpmn:documentation>"
    xml = base.replace(marker, marker + doc, 1)
    n = len(xml.encode("utf-8"))
    assert n > MYSQL_TEXT_LIMIT, f"构造 XML {n}B 未超过 64KB，无法验证"
    return xml


def probe_password(args: argparse.Namespace) -> str:
    """返回可用 MySQL root 密码；找不到则退出。"""
    import pymysql

    candidates: list[str] = []
    for pw in (os.environ.get("MYSQL_PW"), args.pw, "", "root", "123456", "password", "camunda", "mysql"):
        if pw is not None and pw not in candidates:
            candidates.append(pw)
    for pw in candidates:
        try:
            c = pymysql.connect(
                host=args.host, port=args.port, user=args.user,
                password=pw, connect_timeout=3,
            )
            c.close()
            return pw
        except pymysql.MySQLError:
            continue
    raise SystemExit(
        f"无法连接 MySQL {args.user}@{args.host}:{args.port}（常见密码均被拒），"
        f"请用 --pw 或环境变量 MYSQL_PW 指定密码"
    )


def run_round(engine, label: str) -> None:
    """启动一轮自动通过场景（小额 -> COMPLETED），断言走完。"""
    pi = engine.start_process_instance_by_key(
        "loan-approval", {"applicant": "big-xml", "amount": 5000}
    )
    assert pi.state.value == "COMPLETED", f"{label}: 小额应自动通过并结束"
    assert pi.variables.get("credit_ok") is True
    print(f"[{label}] start -> COMPLETED OK, id={pi.id}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--local", action="store_true", help="SQLite 冒烟（不连 MySQL）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=3306)
    ap.add_argument("--user", default="root")
    ap.add_argument("--pw", default=None, help="MySQL 密码（缺省自动探测）")
    ap.add_argument("--db", default="camunda_py_test")
    args = ap.parse_args()

    xml = build_big_xml()
    print(f"BPMN XML size = {len(xml.encode('utf-8'))} bytes"
          f" (MySQL TEXT limit = {MYSQL_TEXT_LIMIT})")

    if args.local:
        from camunda.engine import ProcessEngine
        from camunda.parser import parse_bpmn_xml

        db = ROOT / "big_xml_smoke.db"
        if db.exists():
            db.unlink()
        from camunda.persistence.store import Store
        url = f"sqlite:///{db}"
    else:
        import pymysql
        from urllib.parse import quote_plus

        pw = probe_password(args)
        conn = pymysql.connect(
            host=args.host, port=args.port, user=args.user,
            password=pw, charset="utf8mb4", autocommit=True,
        )
        cur = conn.cursor()
        cur.execute(f"DROP DATABASE IF EXISTS `{args.db}`")
        cur.execute(
            f"CREATE DATABASE `{args.db}` CHARACTER SET utf8mb4 "
            f"COLLATE utf8mb4_unicode_ci"
        )
        cur.close()
        conn.close()
        url = (
            f"mysql+pymysql://{args.user}:{quote_plus(pw)}"
            f"@{args.host}:{args.port}/{args.db}?charset=utf8mb4"
        )

    # ---- 1) 建表 + 部署超 64KB XML ----
    from camunda.engine import ProcessEngine
    from camunda.parser import parse_bpmn_xml
    from camunda.persistence.store import Store

    store = Store(url)
    engine = ProcessEngine(store=store)
    engine.register_delegate("checkCredit", lambda v: v.update(credit_ok=True))
    model = parse_bpmn_xml(xml, source_name="big-loan.bpmn")
    engine.deploy(model, name="big-loan")
    print("deploy OK")

    # ---- 2) 读回逐字节一致 ----
    back = {d["key"]: d for d in store.load_proc_defs()}
    stored = back["loan-approval"]
    assert stored["xml"] == xml, "读回的 XML 与原串不一致"
    print(f"read-back OK: version={stored['version']}, "
          f"xml_len={len(stored['xml'].encode('utf-8'))}")

    # ---- 3) 一轮启动走完（覆盖 RU/HI 写库）----
    run_round(engine, "deploy-engine")

    # ---- 4) 崩溃恢复语义：全新引擎 from_database 重解析 + 再启动 ----
    engine2 = ProcessEngine.from_database(url)
    engine2.register_delegate("checkCredit", lambda v: v.update(credit_ok=True))
    print("from_database OK")
    run_round(engine2, "restored-engine")

    # ---- 5) MySQL：information_schema 确认列类型（MEDIUMTEXT 16MB）----
    if not args.local:
        cur = conn_ro = None
        import pymysql
        info = pymysql.connect(
            host=args.host, port=args.port, user=args.user,
            password=pw, database=args.db, charset="utf8mb4",
        )
        for table, column in (
            ("ACT_RE_PROCDEF", "RESOURCE_XML_"),
            ("ACT_RU_VARIABLE", "TEXT_"),
            ("ACT_HI_VARINST", "TEXT_"),
        ):
            cur = info.cursor()
            cur.execute(
                "SELECT COLUMN_TYPE, CHARACTER_MAXIMUM_LENGTH "
                "FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND COLUMN_NAME=%s",
                (args.db, table, column),
            )
            row = cur.fetchone()
            assert row and "mediumtext" in row[0].lower(), (
                f"{table}.{column} 应为 mediumtext，实际 {row}"
            )
            print(f"column {table}.{column} = {row[0]} "
                  f"(max_chars={row[1]}) OK")
        info.close()

    engine.close()
    if not args.local:
        engine2.close()
        print(f"PASS: MySQL 全链路验证通过（测试库保留: {args.db}）")
    else:
        print("PASS: SQLite 冒烟通过")
        try:
            db.unlink()  # Windows/IDE 拦截删除时容忍（残留仅本地冒烟库）
        except OSError:
            print(f"note: 未能自动清理临时库 {db.name}，可手动删除")


if __name__ == "__main__":
    main()
