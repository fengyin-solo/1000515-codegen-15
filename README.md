# 地质勘探数据管理平台

面向地质勘探的钻孔编录、岩心取样、物探数据、化探分析、测绘资料与储量估算的综合数据管理后台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 钻孔编录 | `borehole` | 钻孔 | 钻孔编号、勘探区、孔口坐标 |
| 岩心管理 | `core` | 岩心样本 | 岩心编号、所属钻孔、取样深度起 |
| 地层划分 | `stratigraphy` | 地层单元 | 单元编号、钻孔编号、地层名称 |
| 地球物理 | `geophysics` | 物探测线 | 测线编号、勘探区、物探方法 |
| 化探分析 | `geochem` | 化探样品 | 样品编号、样品类型、采样点位 |
| 化验数据 | `assay` | 化验结果 | 化验编号、样品编号、元素名称 |
| 地质填图 | `mapping` | 填图单元 | 图幅编号、图幅名称、比例尺 |
| 测绘控制 | `survey_point` | 控制点 | 点号、点类型、坐标X |
| 钻探日志 | `drilling_log` | 钻探记录 | 日志编号、钻孔编号、钻进深度 |
| 储量估算 | `reserve` | 矿体块段 | 块段编号、矿体名称、面积 |
| 样品登记 | `sample_registry` | 送检样品 | 送检编号、样品名称、采样位置 |
| 勘探设备 | `equipment` | 勘探仪器 | 仪器编号、仪器名称、型号规格 |
| 水文地质 | `hydro` | 水文观测点 | 观测编号、观测类型、所在钻孔 |
| 剖面编录 | `section` | 实测剖面 | 剖面编号、剖面名称、剖面长度 |
| 地质报告 | `geological_report` | 勘探报告 | 报告编号、勘探区、报告类型 |
| 遥感解译 | `remote` | 遥感数据 | 数据编号、数据源、分辨率 |
| 矿产评价 | `mineral` | 矿化线索 | 线索编号、勘探区、矿种 |
| 环境地质 | `environmental` | 环境调查点 | 调查编号、调查区域、灾害类型 |

### 剖面批次打包台

实测剖面的「图属相符性导出」不再是一份孤立文件，而是成批出包，相关规则集中在
`app/services/section_pack.py`，接口挂在 `/api/section` 下：

- **出包前确认**：`POST /api/section/{id}/layers/confirm` 先在剖面上确认剖面分层与剖面长度；
  分层累计长度与申报长度不相符时以审定边界为准（可携带「审定边界」现场裁定），写回审定长度。
- **三件齐全才出包**：每个剖面必须同时产出数据包、图纸清单、核验摘要；任一缺失（分层未确认、
  缺图幅号、外部核对清单未核等）整批不出包，批次状态置为「已驳回」并逐条说明原因，下载返回 409。
- **三处来源同步**：核验摘要与数据包都带剖面详情、地层台账（按分层名称匹配同名单元）、外部核对
  清单的来源留痕（条目数与哈希）；剖面详情接口同时返回三处来源与历史导出版本。
- **事务化与版本**：导出、存档、版本号分配在 `store.transaction(...)` 同一事务内完成；
  重复导出内容哈希相同则复用同一版本（只保留一套版本），内容变化才递增版本，历史快照不可变。
- **断线续传**：`POST /api/section/batches/{id}/resume` 接着原批次继续，已出包剖面不重做；
  服务启动时把排队中/导出中的批次重新入队。
- **异步队列与稳定分页**：超过阈值（`ASYNC_THRESHOLD`）的批次走后台队列；创建批次时冻结
  按剖面 id 升序的清单，后续翻页/出包都按冻结清单走，新增剖面不影响在途批次。
- **历史补图幅号**：启动时给缺图幅号的历史剖面按原剖面编号稳定映射补入图幅号，保留原 id 与
  剖面编号，迁移日志见 `GET /api/section/migration`，可用 `POST /migration/backfill` 重入执行。
- **整包下载**：`GET /api/section/batches/{id}/download` 返回 zip，内含每个剖面的三件文件，
  以及整批的 `批次清单.json` 与 `核验总摘要.json`。

辅助表（`section_layer`、`section_checklist`、`section_export_batch`、
`section_export_item`、`section_archive`、`section_migration_log`）不计入运营概览的业务模块数。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。
