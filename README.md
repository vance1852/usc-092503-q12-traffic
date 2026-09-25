# 道路交通事故快处与处罚协同服务

本项目是一套可离线运行的 Python 后台，用于事故受理、道路风险研判、警力与拖车调度、结构化证据复核、责任认定、处罚执行和审计追溯。系统把同一事故从报警到结案的关键状态保存在 SQLite 中，角色权限覆盖接警员、调度员、事故处理民警、复核人员和审计人员。

## 目录

- `src/traffic_dispatch/`：事故风险指数、快处中心、道路走廊、应急资源、调度申请和响应情景；
- `src/evidence_review/`：采集设备、证据规范、结构化记录导入、一致性分析、复核租约和采信决定；
- `src/penalty_ops/`：事故案件、违法记录、风险告警、处置工单、处罚流转和审计；
- `src/penalty_appeal/`：处罚申诉登记与合并、材料补正、受理审查、审查人员回避、复核决定、撤回、执行中止与台账联动、逾期登记和送达留痕；
- `fixtures/`：离线验收使用的证据规范与结构化事故记录；
- `tests/`：领域规则、事务边界、权限、HTTP API 和 CLI 验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅依赖 Python 标准库与 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -q
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m traffic_dispatch.acceptance --workspace .
PYTHONPATH=src python3 -m evidence_review.acceptance --workspace .
PYTHONPATH=src python3 -m penalty_ops.acceptance
PYTHONPATH=src python3 -m penalty_appeal.acceptance
```

验收会建立临时 SQLite 数据库，登记事故风险记录、快处中心、道路走廊和应急资源，完成调度与证据复核，并输出 JSON 结果。命令不会访问公网，也不需要额外数据库、队列或常驻服务。

## HTTP API

```bash
PYTHONPATH=src python3 -m traffic_dispatch.api --database traffic.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m evidence_review.api --database evidence.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m penalty_ops.api --database penalties.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m penalty_appeal.api --database penalty_appeal.sqlite3 --host 127.0.0.1 --port 8083
```

申诉服务覆盖以下流转与规则：

- 只有处罚决定的当事人本人，或持有效授权（区分发起、撤回、全权，可限定决定范围与有效期）的代理人可以发起申诉；
- 同一处罚决定、同一法定事由存在未终结申诉时，重复提交自动合并为同一案件（追加申请人、材料和证据版本），不另立多案；
- 材料补正通知设补正期限，逾期未补正按撤回处理；受理后按可配置的 `suspension_rules` 中止相应执行动作（如罚款催缴、暂扣证照），催缴流程直接读取执行动作状态，记分等不自动中止的动作按规则保持执行；
- 复核结论为维持、变更或撤销：维持恢复执行，撤销在同一事务内终结全部执行动作，变更按调整项修改金额/期限或终结单项，未涉及的中止动作恢复；
- 全部期限（申请期、受理审查期、补正期、复核期）由可注入时钟计算；逾期未解释不能继续流转，逾期原因经 `POST /overdue/detect` 与逾期说明登记后可查询；
- 审查人员指派自动排除原决定承办人，支持当事人申请回避（管理员决定）与自行回避；
- 引用证据按 `evidence_id + evidence_version + SHA-256` 固化进复核决定快照；最终送达记录后案件进入 `served`，各阶段送达均可查询；
- 所有状态变化写入前向哈希链审计表 `appeal_audit_events`，`GET /audit/chain` 可校验篡改。

四个服务均提供 `GET /health`，其余接口使用 JSON。SQLite 文件保存业务状态、幂等结果和审计记录，进程重启后可继续查询。
