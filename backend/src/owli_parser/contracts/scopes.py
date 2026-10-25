"""scope 注册表（协议不变量）。

集中定义外部应用 Token 可授的 scope 名称与资源约束；
前端从 /v1/scopes 读取元信息，不另造名单。
"""

from __future__ import annotations

from dataclasses import dataclass

SCOPES_REGISTRY_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class ScopeDefinition:
    name: str
    description: str
    category: str
    items: str
    requires_dataset_grant: bool
    resource_actions: tuple[str, ...] = ()


SCOPE_REGISTRY: dict[str, ScopeDefinition] = {
    defn.name: defn
    for defn in (
        ScopeDefinition(
            name="datasets:read",
            description="读取授权数据集列表与详情",
            category="数据集",
            items="列表不得返回其他 ID/数量",
            requires_dataset_grant=True,
        ),
        ScopeDefinition(
            name="documents:read",
            description="读取文档/版本/blocks/chunks/IR 元数据",
            category="文档",
            items="所属数据集 read 授权；完整 IR/正文仍按资源权限检查",
            requires_dataset_grant=True,
            resource_actions=("read",),
        ),
        ScopeDefinition(
            name="documents:write",
            description="文档命名、创建版本、chunk 编辑/禁用",
            category="文档",
            items="write 授权；危险变更使用 If-Match",
            requires_dataset_grant=True,
            resource_actions=("write",),
        ),
        ScopeDefinition(
            name="documents:delete",
            description="删除文档",
            category="文档",
            items="独立 delete 授权，不由 write 隐含授予",
            requires_dataset_grant=True,
            resource_actions=("delete",),
        ),
        ScopeDefinition(
            name="imports:create",
            description="上传 init/part/complete、创建 URL/对象导入",
            category="接入",
            items="write 授权；上传会话从 init 绑定 dataset_id",
            requires_dataset_grant=True,
            resource_actions=("write",),
        ),
        ScopeDefinition(
            name="imports:read",
            description="读取导入/上传状态",
            category="接入",
            items="同应用创建且数据集仍授权",
            requires_dataset_grant=True,
            resource_actions=("read",),
        ),
        ScopeDefinition(
            name="jobs:create",
            description="提交解析任务；导入时自动提交解析",
            category="任务",
            items="write 授权；同时检查 artifact 与 pipeline 可用性",
            requires_dataset_grant=True,
            resource_actions=("write",),
        ),
        ScopeDefinition(
            name="jobs:read",
            description="读取任务快照与事件",
            category="任务",
            items="同应用任务；共享文档状态通过 documents:read 获取",
            requires_dataset_grant=False,
        ),
        ScopeDefinition(
            name="jobs:control",
            description="取消/重试任务",
            category="任务",
            items="同应用任务 + write 授权",
            requires_dataset_grant=True,
            resource_actions=("write",),
        ),
        ScopeDefinition(
            name="artifacts:read",
            description="原件下载、预览内容、导出文件流",
            category="产物",
            items="read 授权；不等于列举整个 bucket",
            requires_dataset_grant=True,
            resource_actions=("read",),
        ),
        ScopeDefinition(
            name="retrieval:query",
            description="召回测试接口",
            category="检索",
            items="read 授权；提交数据集必须全部获授权",
            requires_dataset_grant=True,
            resource_actions=("read",),
        ),
        ScopeDefinition(
            name="chat:invoke",
            description="对话接口与本人应用 run 事件/取消",
            category="对话",
            items="read 授权、模型用途授权及费用预算",
            requires_dataset_grant=True,
            resource_actions=("read",),
        ),
    )
}

# Token 不开放系统设置、模型密钥、Token 签发和成员管理 scope
DATASET_ACTIONS: tuple[str, ...] = ("read", "write", "delete")


def validate_scope_names(names: list[str]) -> list[str]:
    """返回未知 scope 列表（空表示全部合法）。"""
    return [n for n in names if n not in SCOPE_REGISTRY]
