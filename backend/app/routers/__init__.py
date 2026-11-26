"""业务路由汇总：通用模块共用资源路由，分片发布走受控编排路由。"""
from __future__ import annotations

from app.routers import resource, shard_publish

# 分片发布路由必须先注册：通用资源路由的 /api/{module}/{entry_id}
# 会把 /api/shard-publish/... 误当成模块条目查询。
ROUTERS = [shard_publish, resource]
