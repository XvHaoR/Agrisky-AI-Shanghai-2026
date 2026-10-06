# 复现、运行与排障

## A. 本地合成流程（无外部账号）

按照根 README 安装 Python 与 npm 依赖。运行 `scripts/demo_server.py` 前设置至少 12 位的 `AGRISKY_DEMO_PASSWORD`。脚本只监听 127.0.0.1:8000，默认持久化目录为 `.runtime/shanghai-demo/`，使用示例保单和固定模拟 SAR 结果，关闭实际 GEE 与外部模型调用。该入口是演示夹具，不能用于真实业务。

前端在 `frontend-next` 中构建并监听 127.0.0.1:3000。账号 `reviewer`。运行 `scripts/demo_replay.py` 将新建合成案件，先验证越级规则调用被拒绝，再完成材料、遥感、合规、规则、报告和合成审核归档。每次运行创建新案件。默认输出 `.runtime/demo-replay.json`，不输出 Cookie 或口令。

合成模式放宽了材料上传要求，并将遥感服务替换为确定性夹具；HTTP、数据库、状态机、报告和审核实现仍为真实代码。它不证明模型工具选择能力，也不验证真实影像获取。报告中的模拟标签应保留。

## B. 外部模型和真实 GEE 模式

```powershell
Copy-Item .env.example .env
# 编辑 .env，填写自己的账号、模型与 GEE 配置
python -m uvicorn main:app --app-dir api_gateway --host 127.0.0.1 --port 8000
```

主要变量：`AGRISKY_BOOTSTRAP_ADMIN_USERNAME/PASSWORD` 首次创建管理员；`AGENT_PROVIDER=openai_compatible`、`AGENT_BASE_URL`、`AGENT_MODEL`、`AGENT_API_KEY` 配置模型；`GEE_PROJECT_ID`、`GEE_CREDENTIALS` 配置已开通 Earth Engine 的项目与凭据路径。不要将密钥写入 NEXT_PUBLIC 变量。

保持 `AGRISKY_ALLOW_MOCK_REMOTE_SENSING=false`、`AGRISKY_REQUIRE_CLAIM_DOCUMENTS=true`，正式数据关闭 `AGRISKY_SEED_DEMO_DATA`。请配置独立的 `AGRISKY_DB_PATH` 与 `AGRISKY_OUTPUT_ROOT`。生产 HTTPS 下开启安全 Cookie、收窄 CORS，禁止直接公开输出目录。首次初始化后移除引导密码。

Docker Compose 也可用于部署，但需要自行将 GEE 凭据文件以只读卷挂载到容器并配置容器内路径，不能直接使用 Windows 宿主机路径。不要将凭据提交到 Git。当前验收为 Windows 本地启动，Docker 干净部署以 CI 的容器任务结果为准。

## C. 页面顺序

1. 保单与地块：登记保单，上传已授权边界及合同并冻结版本。
2. 案件队列：按在册保单创建案件，绑定出险日期、灾害和地块。
3. Agent 助理：配置真实模型后用自然语言调度；工具状态、来源和错误都保留。无模型时不要假装自动调度已成功。
4. 理赔驾驶舱：查看材料、SAR、长势、合规和规则结果；错误应先修复输入或服务条件。
5. 报告与审计：生成草稿，具名审核员核对批次、快照、附件摘要后归档。

## D. 常见问题

- 端口占用：停止本次服务或换一组对应端口；本合成脚本固定 8000/3000，不要覆盖已有生产服务。
- 登录失效：同一账号新登录会撤销旧会话；回放脚本运行后重新登录。
- GEE 失败：检查项目权限、凭据、配额与网络；生产模式保留错误，不开启模拟来绕过。
- PDF 文本行无矩形框：上海版原生 PDF 保留页码/行号引用，矩形框暂为空；OCR 页面保留识别框。
- Windows 中文图表：确保存在中文字体；容器建议安装 Noto CJK。
- npm 审计：生产依赖与开发依赖分别评估；不要直接使用 `npm audit fix --force` 引入跨主版本变更。

### 可选：合成数据上的真实模型演示

默认演示不调用外部模型。若希望验证自然语言调度，在启动演示服务器前显式设置 `AGRISKY_DEMO_ENABLE_MODEL=true`，并通过环境变量提供自己的 `AGENT_API_KEY`、`AGENT_BASE_URL`、`AGENT_MODEL`，以及仅本机工具网关使用的 `AGRISKY_AGENT_API_KEY`。只发送合成案例摘要；遥感仍为模拟，不会因此变成真实GEE验证。调用会使用部署者的模型配额。不要提交密钥或演示数据库。
