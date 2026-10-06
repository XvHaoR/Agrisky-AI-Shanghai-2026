# 代码、依赖与数据来源

根目录 LICENSE 适用于团队自研代码，不覆盖第三方软件、地图瓦片、影像和业务数据。

| 来源 | 用途与处理 |
| --- | --- |
| Leaflet | 前端地图运行资源；保留 `api_gateway/vendor/leaflet/LICENSE` 原始 BSD 声明。 |
| pypdfium2 / PDFium | PDF 解析与扫描页栅格化；pypdfium2 的 BSD-3-Clause / Apache-2.0 与 PDFium 的第三方许可分别适用，依赖包自带完整许可。部署或再分发二进制时保留包内许可证。本版本不再依赖 PyMuPDF。 |
| Next.js / React / FastAPI 等 | 通过包管理器安装，不将 node_modules、虚拟环境或二进制依赖装入源码包；版本与声明见清单，完整许可见上游包。 |
| Sentinel / Earth Engine | 调用部署者获准使用的服务；影像与服务条款不会因本项目 Apache-2.0 许可而改变。使用者负责项目准入、配额和数据权利。 |
| 地图底图与在线资源 | 遵循各服务的署名和使用规则；不默认授予批量下载或再分发权利。 |
| Sen1Floods11 | 公开研究基准；本仓库保存实验脚本、样本划分和结果，不打包原始影像。按原项目数据许可下载、署名和使用。 |
| 法规资料 | `legal_corpus/manifest.json` 保留官方来源和检索信息。法规整理材料仅作辅助引用，使用时核查有效性及适用范围，不能替代专业审查。 |
| 示例地块与保单 | `data/sample_cases` 为演示几何，演示服务器生成示例业务数据，不构成对任何真实土地的权利或承保声明。 |

依赖记录：`docs/dependencies/python-direct.json`（本机直接依赖版本与包声明）和 `docs/dependencies/npm-lock.json`（npm 锁文件声明）。这是可核对的依赖清单，不是对所有传递依赖的法律结论。不同平台解析出的 Python 版本可能不同。

公开分发前剔除 `.env`、服务账号、上传材料、运行数据库、运行报告及工作缓存。再分发容器或内置模型时应重新检查实际安装组件和模型权利；本次仅分发自研源码、文档和合成演示。

上游：
- https://github.com/pypdfium2-team/pypdfium2
- https://pypdfium2.readthedocs.io/en/stable/python_api.html
- https://github.com/cloudtostreet/Sen1Floods11
- https://developers.google.com/earth-engine/datasets
