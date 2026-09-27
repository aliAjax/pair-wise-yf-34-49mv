# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。

备降协同解决夜间改航线时"不知道备降点能不能接"的问题：审核员维护备降点的适用机型、开放时段和同时容量；计划登记主/备备降点；批准时占用主点名额，机型不匹配、超出开放时段或满员则拒绝并返回当前占用情况；指挥官紧急改降可切到备用点（主点立即释放），备用点接不下则保留原安排；计划变更、取消和到期会同步释放名额。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区。
- `POST /api/plans`：创建飞行计划；可带 `primary_alternate_id`、`backup_alternate_id` 登记主/备备降点。
- `GET /api/plans/{id}/check`：检查硬约束和相邻交通冲突。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等。批准时校验主备降点的机型、开放时段与同时容量，通过后占用主点名额，失败返回 `alternate_unavailable` 和占用清单。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消，并生成通知；占用名额同步释放（变更后需重新提交、重新批准）。
- `POST /api/plans/{id}/divert`：指挥官/审核员紧急改降到备用备降点（可在 `target_alternate_id` 显式指定，默认用计划登记的备用点）；成功释放主点名额并占用备用点，备用点不匹配/超时段/满员时返回 `diverted:false` 并保留原安排，两种结果都写入改降记录和通知。
- `POST/GET /api/diversion-points`：审核员或指挥官维护备降点（代码、名称、适用机型 `models`（`["*"]` 表示不限）、同时容量 `capacity`、开放时段 `opens_at/closes_at`、状态）；`POST /api/diversion-points/{id}/update` 更新。
- `GET /api/diversion-board`：备降协调台，返回各点余量（`remaining_now`、`occupied_now`）、占用计划和改降记录；运营方只看本运营方明细，viewer 只看公开余量。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理（到期释放备降名额）。
- `GET /api/state`：按角色返回计划、限制和公开信息。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

空域几何使用经纬度矩形和航线包围盒近似，不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite 适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
