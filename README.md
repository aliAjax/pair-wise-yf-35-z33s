# 反兴奋剂检测与结果管理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8301`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

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
python3 app.py --db ./data.db --port 8301
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `athlete`：运动员；`sample`：检测样本；`case`：结果管理案件。
- `tue`：治疗用药豁免（TUE），独立记录，不再写在备注中。

## 治疗用药豁免（TUE）

- 登记：医生（`doctor`）或管理员通过 `POST /api/tues` 登记，必填 `athlete_id`、`substances`（物质列表，也支持单数 `substance`）、`valid_from`、`valid_to`（`YYYY-MM-DD`，含首尾两天）。新豁免初始状态为 `pending`。
- 独立审批：`approve` 后豁免变为 `active` 才生效；审核人必须是 `panel`/`admin`，且其 `X-User-Id` 不能与登记申请人相同，否则返回 403。`reject`（须填 `reason`）后为 `rejected`。
- 作废与重新申请：`active` 的豁免可由 `panel`/`admin`/医生执行 `revoke`（须填 `reason`）。重新申请请新建一条 `tue`，可用 `replaces_tue_id` 关联旧记录；旧记录（pending/rejected/active/revoked 各版本）始终保留，可通过 `GET /api/tues?athlete_id=<id>` 查看该运动员的历次豁免。
- 阳性核对：实验室执行 `analyze` 时可在 `substances` 中登记检出物质，执行 `report_adverse` 时按样本 `collected_at` 的采样日期核对——该日处于生效窗口内、且检出物质被一份或多份有效豁免并集覆盖的，样本状态标为 `protected`（受保护），判定结果及豁免快照冻结在样本的 `tue_determination` 中；否则为 `adverse`。
- 不可开案：受保护样本无法创建案件（`POST /api/cases` 返回 400）。
- 判定不可追溯改写：豁免的事后批准、作废或重新申请都不会修改已报出样本上的冻结判定与状态。
- 结果视图：`GET /api/results`（可带 `?athlete_id=`）列出已分析样本的结果、`protected` 标记、覆盖豁免快照和已开案件 ID，可直接看出哪些结果受保护。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤，`tues`/`samples`等还支持`?athlete_id=`过滤。
- `GET /api/results`：结果管理视图，显示样本结果是否受TUE保护及关联案件。
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

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
