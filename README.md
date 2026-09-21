# FASTQ 质控流水线台（FASTQ QC Pipeline Console）

从零实现的全栈演示：上传/选择小型 FASTQ → **Actor 队列流水线**质控 → 查看阶段状态与指标。

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | Python 3.11 · FastAPI · SQLAlchemy · PostgreSQL |
| 流水线 | `ParseActor` → `QualityHistActor` → `NContentActor` → `ReportActor`（asyncio.Queue） |
| 前端 | Vue 3 · Vite · Quasar · 中文 UI · nginx `/api` 反代 |
| 基建 | docker compose（db / backend / seed / frontend） |

## 端口

| 服务 | 地址 |
|------|------|
| Frontend | http://localhost:3184 |
| Backend API | http://localhost:8184 |
| PostgreSQL | localhost:54384 |

## 账号

| 用户 | 密码 | 权限 |
|------|------|------|
| `bioops` | `fastq123456` | 可提交质控作业，可终止排队/运行中的作业 |
| `auditor` | `audit123456` | 只读结果，不可提交、不可终止 |

## 一键启动

```bash
cd projects/09-fastq-qc-pipeline
docker compose up --build
```

镜像源：Postgres/Node/Nginx 使用 `docker.m.daocloud.io`；npm 使用 `registry.npmmirror.com`；pip 使用清华源。

启动后 seed 会写入：

- `demo-good-r1`：合格样例（可算出 `mean_quality` / `n_rate`）
- `demo-broken-malformed`：损坏样例（`ParseActor` 失败，后续阶段 skipped）

## Verification（验收）

1. 打开 http://localhost:3184 ，用 `bioops` / `fastq123456` 登录。
2. **样例库** 看到 2 条样例 → 选合格样例 **提交质控作业**。
3. 作业详情页看到四个 Actor 阶段均为成功，指标卡出现 `reads` / `mean_quality` / `n_rate`。
4. 再跑损坏样例：`ParseActor` = failed，其余 = skipped。
5. 退出，用 `auditor` / `audit123456` 登录：可看历史与详情，提交作业接口返回 403 / 前端无提交入口；终止接口同样返回 403，页面无终止按钮。
6. 终止作业（bioops）：提交一个作业后**立刻**在详情页点「终止作业」，或在历史页点「终止」；作业状态变为「已取消」，当前阶段 cancelled、后续阶段 skipped，刷新详情/历史仍为已取消，不会变成成功。
7. 已成功 / 已失败 / 已取消的作业再点终止返回 409，前端按钮也不再出现。
8. 健康检查：`curl http://localhost:8184/api/health`

## 作业终止语义

- 仅 `pending`（排队中）/ `running`（运行中）可终止，终态为 `cancelled`（已取消）。
- `success` / `failed` / `cancelled` 不可终止（HTTP 409）；作业不存在返回 404；审计员直调返回 403。
- 终止与流水线执行之间用条件 UPDATE 闭合竞态：运行器只领取 `pending` 作业、只向仍为 `running` 的作业写终态，因此"提交后马上终止"不会被晚到的执行结果覆盖成成功。
- Actor 边界检查取消：已完成的阶段保留成功状态，正在执行边界上的阶段标 cancelled，未开始的阶段标 skipped；详情页与历史列表立即反映。

## API

- `POST /api/auth/login`
- `GET  /api/health`
- `GET  /api/samples`
- `POST /api/jobs` `{ "sampleId": 1 }` 或 `{ "fastqText": "..." }`
- `GET  /api/jobs`
- `GET  /api/jobs/{id}`
- `GET  /api/jobs/{id}/stages`
- `POST /api/jobs/{id}/cancel`（仅 bioops；排队中/运行中 → 已取消）

## 本地单测（可选）

```bash
cd backend
pip install -r requirements.txt
pytest -q
```

覆盖：畸形 FASTQ 在 `ParseActor` 失败；正常样例产出 `mean_quality`。

## 目录结构

```
09-fastq-qc-pipeline/
  PRD.md
  README.md
  docker-compose.yml
  backend/
    Dockerfile
    seed.py
    data/{good,broken}.fastq
    app/
      main.py api.py auth.py models.py schemas.py
      pipeline/{actors,runner}.py
    tests/{test_actors,test_cancel}.py
  frontend/
    Dockerfile nginx.conf
    src/pages/{Login,Samples,JobSubmit,JobDetail,JobHistory}Page.vue
```
