# 基因组数据访问治理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8304`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8304
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `dataset`：受控数据集；`application`：访问申请；`grant`：限时数据使用凭证。
- `committee`：评审委员会；仅管理员可创建，登记委员名单（`members`）与代理投票期限（`proxies`：`delegator`/`proxy`/`starts_at`/`expires_at`），可用`update`动作调整。

## 会议表决

- `review`：按`committee_id`进入审阅，同时锁定有资格委员、利益冲突名单（`conflicts`）和代理登记快照；此后调整委员会不影响本次审阅。
- `vote`：审阅中每位委员提交一次`ballot`（`approve`/`reject`/`recuse`）；冲突委员只能`recuse`；代投传`on_behalf_of`，票数同时记录委托人（`member`）与投票人（`voter`），且代理必须在登记期限内。
- `approve`：无冲突同意达到3票且无反对票才转为`approved`并记录`decision`；票数不足时申请留在`under_review`，`pending_reason`说明缺票原因（缺几票、等谁投、有无反对票）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

数据目录和授权凭证是治理流程演示，不包含真实数据下载、加密或机构身份联邦。
