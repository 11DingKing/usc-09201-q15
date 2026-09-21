# 林地争议调解卷

本项目为集体林权改革与林下产业协作场景提供林地争议调解卷服务：司法所与林业站共同建卷，分层保存主体、争点、证据摘要、调解方案与签收状态，支持争点级共识、版本锁定、历史回放与公开脱敏查询。

## 运行

```bash
python3 -m service.main          # 默认 http://0.0.0.0:3000
python3 -m unittest discover -s tests   # 运行全部测试
```

## 接口

- `GET /health`：健康检查。
- 内部接口（请求头 `X-Actor-Id/X-Actor-Name/X-Actor-Role/X-Actor-Org` 标识调解员，角色须为 `mediator`、机构须为司法所或林业站）：
  - `POST /api/cases`：受理立案，返回卷宗号与公开查询编号。
  - `POST /api/cases/{id}/parties|agents|issues|statements|evidence`：登记主体、授权/更换代理人、争点、陈述（补充只追加）、证据（迟到自动标注）。
  - `POST /api/cases/{id}/sessions` 与 `/sessions/{sid}/conclude`：开始/结束调解会议，并行会议争点不得相交。
  - `POST /api/cases/{id}/plans`：提出方案新版本并锁定依据版本与证据清单。
  - `POST /api/cases/{id}/issues/{iid}/consensus`：争点级达成（`version`+`consenters`）或撤回（`action: withdraw`）。
  - `POST /api/cases/{id}/signoffs|performance`：签收状态与（部分）履行。
  - `POST /api/cases/{id}/litigation-transfer`：未决争点移交诉讼并锁定整卷。
  - `POST /api/cases/{id}/close`：结案。
  - `GET /api/cases/{id}?version=N`：完整卷宗（可查历史版本）。
  - `GET /api/cases/{id}/review?version=N`：结案复核报告（时间线、共识、签收、履行、移交清单、哈希链完整性）。
- `GET /public/progress/{ref}`：公开脱敏进度，不含任何身份、坐落细址、陈述、证据或隐私内容。

领域规则与分层约定见 [docs/domain.md](docs/domain.md)。业务数据与敏感配置应存放在受控环境中；当前存储为进程内实现，鉴权头仅为占位，生产部署须替换为持久化存储与统一鉴权。
