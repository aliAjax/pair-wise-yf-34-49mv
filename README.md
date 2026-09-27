# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。备降协同覆盖夜间改航线场景：审核员维护备降点的适用机型、开放时段和同时容量，计划登记主备降点和备用点，批准时占用主点名额，紧急改降可切到备用点并立即释放主点。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区。
- `POST /api/alternates`、`POST /api/alternates/{id}/update`：审核员维护备降点（适用机型、`HH:MM` 开放时段支持跨午夜、同时容量、启停）。
- `POST /api/plans`：创建飞行计划，可登记 `primary_alternate_id` / `backup_alternate_id`。
- `GET /api/plans/{id}/check`：检查硬约束和相邻交通冲突。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等。批准时占用主备降点名额，机型不匹配、超出开放时段或满员会拒绝并返回该点占用情况。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消，并生成通知；占用名额同步释放。
- `POST /api/plans/{id}/divert`：指挥官或运营方对已批准计划紧急改降。备用点可接收则切换并立即释放主点名额，否则保留原安排；两种结果都记入改降记录。
- `GET /api/alternates/board`：备降协调台，显示各点余量、占用计划和改降记录。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理（到期自动释放备降名额）。
- `GET /api/state`：按角色返回计划、限制和公开信息。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制。备降容量按计划时段重叠计数，开放时段按每日窗口（UTC）判断，不含天气、道面状态等实时可用性。身份头、无签名离线审核以及单机 SQLite 适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
