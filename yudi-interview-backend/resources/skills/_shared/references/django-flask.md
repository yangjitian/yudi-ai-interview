# Django/Flask面试重点（框架+ORM+部署）

## 框架定位与对比
- Django：全栈框架（ORM/Admin/Auth/Template内置），"batteries included"哲学，适合快速交付。
- Flask：微框架（路由+模板引擎核心），扩展生态灵活，适合定制化与微服务。
- 选型：快速MVP/内容管理选Django，API服务/微服务/深度定制选Flask。
- FastAPI：异步原生、Pydantic类型校验、自动生成OpenAPI文档，适合新项目。

## 请求生命周期
- Django：URLconf路由→Middleware链→View函数/类→Template/JSON响应→Middleware链（逆序返回）。
- Flask：Werkzeug WSGI路由→before_request钩子→View函数→after_request钩子→响应。
- 中间件/钩子：认证注入、请求日志、异常捕获、数据库事务管理。

## ORM与查询优化
- Django ORM：QuerySet惰性求值、`select_related`（JOIN，一对一/外键）vs `prefetch_related`（二次查询，多对多）。
- N+1问题：循环中触发懒加载，解法为`select_related`/`prefetch_related`批量预加载。
- Flask-SQLAlchemy：Session管理、eager loading（`joinedload`/`subqueryload`）、批量操作。
- 查询优化：`only()`/`defer()`延迟加载字段、`bulk_create`批量插入、`iterator()`流式读取大结果集。

## 认证、权限与安全
- Django Auth：User模型、`authenticate`/`login`/`logout`流程、Permission/Group权限体系。
- Token认证：JWT（djangorestframework-simplejwt）/ DRF TokenAuthentication。
- 安全防护：CSRF Token（Django内置）、XSS转义（模板自动转义）、SQL注入（ORM参数化）、CORS配置。

## 缓存与性能
- Django缓存框架：`@cache_page`装饰器、缓存后端（Redis/Memcached/本地内存）。
- Flask缓存：Flask-Caching扩展，`@cached`装饰器、Jinja2片段缓存。

## 部署与运维
- WSGI（同步）：Gunicorn/uWSGI，多Worker进程 + preload + 优雅重启。
- ASGI（异步）：Uvicorn/Daphne，支持WebSocket和async view。
- 容器化：Docker多阶段构建、环境变量管理、健康检查端点。
