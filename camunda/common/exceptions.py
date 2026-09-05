"""camunda-python 统一异常层次。

对齐 Camunda 语义：
- CamundaException        引擎根异常（对应 Java ProcessEngineException）
- NotFoundException       对象不存在（对应 ProcessEngineException 派生的 NotFound 语义）
- DeploymentException     部署失败（XML 解析/校验错误）
- ProcessInstanceException 流程实例状态非法操作
- InvalidRequestException 参数/调用不合法
- ExpressionEvaluationException 表达式求值失败（FEEL/UEL，M5）
"""


class CamundaException(Exception):
    """引擎根异常。"""


class NotFoundException(CamundaException):
    """按 id/key 查找对象不存在。"""


class DeploymentException(CamundaException):
    """BPMN 部署失败：XML 格式错误、语义校验不通过。"""


class ProcessInstanceException(CamundaException):
    """流程实例上的非法状态操作（如对已结束实例 start/complete）。"""


class InvalidRequestException(CamundaException):
    """参数或调用不合法。"""


class ExpressionEvaluationException(CamundaException):
    """表达式求值失败：FEEL 语法不支持 / 类型不匹配 / 未定义变量路径（M5）。"""
