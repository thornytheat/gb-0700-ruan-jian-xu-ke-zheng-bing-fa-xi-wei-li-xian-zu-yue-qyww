# 接口

`POST /api/run`：{"text":"CSV内容"}。同规则版本与输入摘要只产生一条批次。

`GET/POST /api/1` 至 `/api/8`：独立样例或自定义文件计算。各模块返回 report、quarantine、balance；M8 另返回 boundary（已发布结论）、publication、idempotent。

`GET /api/boundary/{series}`：某系列当前已发布的分界结论与发布信息。`GET /api/boundary/{series}/revisions`：版本历史与发布尝试（含失败记录）。

`POST /api/review`：id、整数revision、decision取通过或退回。旧版本409；阻断项不能通过。

`GET /api/export/{id}`：唯一证据CSV。`GET /health`：存活检查。

错误：结构/数值422，版本冲突409，不存在404，文件过大413。逐行异常进入quarantine。
