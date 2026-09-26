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
- `exemption`：治疗用药豁免（TUE）独立记录，由医生登记，包含`athlete_id`、`substance`、`valid_from`、`valid_to`，可选`supersedes`指向被取代的旧豁免。

## 治疗用药豁免流程

- 医生（`doctor`角色）登记豁免后状态为`pending`，必须由`reviewer`或`admin`审核；审核人不能与申请人相同，批准后状态变为`active`才生效，`reject`则驳回。
- 实验室对样本执行`report_adverse`时，系统按采样日期（`collected_at`）核对：若该运动员存在`active`且覆盖采样日期的豁免（样本记录了`substance`时还需物质匹配），结果进入`protected`状态，并在样本上固化判定快照（豁免ID、版本、判定时间），不能开案。
- 判定快照在报告阳性时一次性写入；事后作废、重新申请或新增豁免都不会改写已有判定。
- `void`作废豁免（需填`reason`）保留原记录；重新申请新建记录并可用`supersedes`关联旧记录，历次记录和审计时间线均保留。
- `GET /api/results`返回结果管理列表，标明每条阳性/受保护结果的`protected`标记、对应豁免和已开案件。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/results`：结果管理列表（含受保护标记）。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

身份、实验室结果和听证材料均为原型模型，不替代正式反兴奋剂信息系统或证据鉴定流程。
