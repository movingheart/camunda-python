"""部署端点（M6-2）：POST /deployment/create + GET /deployment。

BPMN 与 DMN 共用同一端点：按 XML 根元素的子元素自动分派
（含 decision -> DMN，含 process -> BPMN；两者根元素都叫 definitions）。

对齐 Camunda：multipart/form-data，字段名 `data`，可多文件一次部署。
本项目额外提供 JSON 便捷通道 `POST /deployment/create/xml`（curl/脚本免构造 multipart）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, Query, Request, UploadFile
from lxml import etree

from camunda.api.deps import get_engine
from camunda.api.pagination import DEFAULT_MAX_RESULTS, paginate
from camunda.api.schemas import DeploymentDto, decision_definition_dto, process_definition_dto
from camunda.common.exceptions import DeploymentException, InvalidRequestException
from camunda.parser import parse_bpmn_xml
from camunda.parser.dmn_parser import parse_dmn_xml

router = APIRouter(tags=["deployment"])


def _local(tag: str) -> str:
    """lxml tag 形如 {ns}localName -> localName。"""
    return tag.rsplit("}", 1)[-1]


def _classify(xml: str) -> str:
    """判定 XML 种类：'bpmn' / 'dmn'（无法判定抛 DeploymentException）。"""
    try:
        root = etree.fromstring(xml.encode("utf-8"))
    except etree.XMLSyntaxError as e:
        raise DeploymentException(f"XML 语法错误: {e}") from e
    for child in root:
        if not isinstance(child.tag, str):
            continue
        if _local(child.tag) == "decision":
            return "dmn"
        if _local(child.tag) == "process":
            return "bpmn"
    raise DeploymentException(
        "XML 根元素内未发现 process 或 decision 子元素，无法判定 BPMN / DMN"
    )


def _deploy_xml(engine, xml: str, name: Optional[str] = None) -> Dict[str, List[str]]:
    """部署一份 XML，返回 {"process_keys": [...], "decision_keys": [...]}。"""
    kind = _classify(xml)
    if kind == "bpmn":
        keys = engine.deploy(parse_bpmn_xml(xml, source_name=name), name=name)
        return {"process_keys": list(keys), "decision_keys": []}
    keys = engine.deploy_dmn(parse_dmn_xml(xml, source_name=name), name=name)
    return {"process_keys": [], "decision_keys": list(keys)}


def _deployment_body(
    engine, deployments: List[Dict[str, Any]], name: Optional[str] = None
) -> Dict[str, Any]:
    """聚合若干次部署记录 -> Camunda DeploymentWithDefinitionsDto 形态。"""
    proc_defs: Dict[str, Any] = {}
    dec_defs: Dict[str, Any] = {}
    dep_id = None
    dep_time = None
    for dep in deployments:
        dep_id = dep["id"]
        dep_time = dep["time"]
        for key in dep["process_keys"]:
            version = engine.get_definition_version(key)
            proc_defs[f"{key}:{version}"] = process_definition_dto(
                key, engine.get_process_definition(key).name, version
            )
        for key in dep["decision_keys"]:
            version = engine.get_decision_version(key)
            dec_defs[f"{key}:{version}"] = decision_definition_dto(
                key, version, engine.get_decision_definition(key).name
            )
    return {
        "id": dep_id,
        "name": name,
        "time": dep_time,
        "deployedProcessDefinitions": proc_defs,
        "deployedDecisionDefinitions": dec_defs,
    }


@router.post("/deployment/create", summary="部署 BPMN/DMN XML（multipart）")
async def create_deployment(
    request: Request,
    data: Optional[List[UploadFile]] = File(default=None, alias="data"),
) -> Dict[str, Any]:
    """multipart 部署，字段名 `data`，可多文件（对齐 Camunda 7 REST）。"""
    engine = get_engine(request)
    if not data:
        raise InvalidRequestException(
            "缺少上传文件：form-data 字段名须为 data，可重复携带多个 .bpmn / .dmn"
        )
    deployments: List[Dict[str, Any]] = []
    for upload in data:
        content = (await upload.read()).decode("utf-8")
        before = len(engine.list_deployments())
        _deploy_xml(engine, content, name=upload.filename)
        deployments.extend(engine.list_deployments()[before:])
    return _deployment_body(engine, deployments)


@router.post("/deployment/create/xml", summary="部署 BPMN/DMN XML（JSON 便捷通道）")
def create_deployment_xml(request: Request, body: DeploymentDto) -> Dict[str, Any]:
    """JSON 便捷通道（本项目扩展）：body = {"xml": "...", "name": "..."}。"""
    engine = get_engine(request)
    before = len(engine.list_deployments())
    _deploy_xml(engine, body.xml, name=body.name)
    return _deployment_body(engine, engine.list_deployments()[before:], name=body.name)


@router.get("/deployment", summary="部署列表")
def list_deployments(
    request: Request,
    firstResult: int = Query(default=0, ge=0, description="分页起点（0 基）"),
    maxResults: int = Query(
        default=DEFAULT_MAX_RESULTS, ge=1, description="分页上限（<=1000）"
    ),
) -> List[Dict[str, Any]]:
    engine = get_engine(request)
    items = [
        {
            "id": d["id"],
            "name": d["name"],
            "time": d["time"],
            "source": d["source"],
            "process_keys": list(d["process_keys"]),
            "decision_keys": list(d["decision_keys"]),
        }
        for d in engine.list_deployments()
    ]
    return paginate(items, firstResult, maxResults)
