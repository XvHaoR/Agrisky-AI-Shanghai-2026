# 穹野智保 Agrisky AI

农业保险理赔协同开源工具 · 2026 上海开源软件应用创新大赛 · 开源 AI 工具赛道（自主项目）

**RsKing 团队｜负责人：许浩然（中山大学）**

穹野智保把保单合同、材料核验、遥感证据、确定性赔付规则、报告与人工审核连接成可追溯工作流。模型调用 20 项白名单工具，业务引擎计算关键数值，最终归档由具名审核员完成。本工具提供辅助建议，不作自动赔付决定。

- [作品介绍与演示视频（Release 附件）](https://github.com/XvHaoR/Agrisky-AI-Shanghai-2026/releases/tag/v1.0.0-shanghai)
- [快速复现与配置](docs/REPRODUCE.md)
- [开源许可与数据边界](THIRD_PARTY_NOTICES.md)
- [贡献指南](CONTRIBUTING.md) · [安全报告](SECURITY.md) · [路线图](ROADMAP.md)
- [公开遥感实验](validation/README.md) · [原系统详细手册](README_UPSTREAM.md)

## 先看什么

业务入口：保单与地块 → 案件队列 → Agent 助理／理赔驾驶舱 → 报告与审计。

```text
Next.js 工作台 → FastAPI／工具网关 → 状态机／合同／遥感／规则引擎
                                          ↓
                     报告草稿 → 具名人工审核 → 归档回执与摘要校验
```

## 在本地启动合成演示

需要 Python 3.11+、Node.js 22 和 npm。以下模式只绑定回环地址，使用独立数据库、合成保单和固定模拟遥感结果，不需要 GEE 或模型密钥。

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
$env:AGRISKY_DEMO_PASSWORD = '请替换为至少12位的本地演示密码'
python scripts/demo_server.py
```

另开终端：

```powershell
cd frontend-next
npm ci
npm run build
npm run start -- --hostname 127.0.0.1 --port 3000
```

访问 http://127.0.0.1:3000 ，账号 `reviewer`，密码为自己设置的演示密码。第三个终端设置同一 `AGRISKY_DEMO_PASSWORD` 后运行 `python scripts/demo_replay.py`，可复现完整合成案件链路及跳步拦截。回放会使同账号旧会话失效，浏览器重新登录即可。不要将该演示入口暴露到公网。

## 真实模型与遥感

复制 `.env.example` 为 `.env`，填写自己的管理员密码、OpenAI-compatible 模型配置及 Earth Engine 项目和凭据。按 `docs/REPRODUCE.md` 的生产模式命令启动；使用在册、已授权地块及材料。默认关闭遥感模拟回退，并开启材料要求。演示服务器与正式启动入口分开，不得用合成模式结果支撑真实案件。

## 验证范围

本版本包含后端回归、前端检查和构建、PDF 解析回归及本地 HTTP 合成链路回放。具体结果见 `docs/VERIFICATION.md`。Sen1Floods11 的 30 个独立测试样本上，SAR 融合 v2 micro F1 为 0.7960、IoU 为 0.6611；仅验证水体分割，不代表农业灾损、业务试点或赔款准确率。

上海版将 PDF 解析依赖替换为 pypdfium2，保留原生文本页码和行号引用，文本矩形框暂不提供；扫描页仍使用 RapidOCR。自研代码采用 Apache-2.0，第三方组件及数据分别遵循各自条款。本仓库不包含密钥、运行数据库、用户上传或真实投保人材料。

### 可选：合成数据上的真实模型演示

默认演示不调用外部模型。若希望验证自然语言调度，在启动演示服务器前显式设置 `AGRISKY_DEMO_ENABLE_MODEL=true`，并通过环境变量提供自己的 `AGENT_API_KEY`、`AGENT_BASE_URL`、`AGENT_MODEL`，以及仅本机工具网关使用的 `AGRISKY_AGENT_API_KEY`。只发送合成案例摘要；遥感仍为模拟，不会因此变成真实GEE验证。调用会使用部署者的模型配额。不要提交密钥或演示数据库。
