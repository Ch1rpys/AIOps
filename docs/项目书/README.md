# AIOps 技术设计与实现说明 · 交付目录

本目录是《AIOps 故障诊断与处置验证平台——技术设计与实现说明》的源文件与交付件。

## 交付件

| 文件 | 说明 |
| --- | --- |
| `AIOps故障诊断与处置验证平台_技术设计与实现说明.docx` | Word 版，71 页，可编辑；目录页码为静态，正文页码由 PAGE 域生成 |
| `AIOps故障诊断与处置验证平台_技术设计与实现说明.pdf` | 排版定稿，71 页；建议提交或打印这一版 |
| `content/*.md` | 七章正文与四个附录的可编辑源文 |
| `assets/` | 概念图、界面截图、封面 logo |
| `build/` | 资产生成、DOCX 构建与目录页码映射脚本 |

## 文档结构

- 第一至七章：问题定义、总体架构、故障编译器与影子世界、多智能体编排、验证与评测、核心实现与界面、测试与运行效果。
- 附录 A：39 个接口分组与常见错误码。
- 附录 B：复现、验收与 API 示例命令。
- 附录 C：术语表。
- 附录 D：第三方组件与许可。

## 数据口径

- 正文中的工单、评测、影子与审计数字取自本机 `data/platform.sqlite3`，截至 2026-11-19。
- 界面截图取自 2026-11-17 本机真实运行环境（`docs/figures/`），不是设计稿。
- 后端测试数字来自 `pytest -q`：380 passed / 31.88s；前端 lint/typecheck/build 通过；Ruff 仍有 62 条风格告警，已在 7.4 / 7.7 节如实说明。
- 文档不包含管理员令牌、数据库密码或模型密钥。

## 重新构建

在仓库根目录执行。`SHOT_PY` 指带 Playwright、Pillow 的解释器（本项目为 `.tools/shotenv/bin/python`）；`DOC_PY` 指带 `python-docx`、`Pillow` 的解释器。

```bash
# 1. 生成概念图、裁切截图、渲染 logo
$SHOT_PY docs/项目书/build/make_assets.py

# 2. 第一遍构建（目录页码为“—”）
$DOC_PY docs/项目书/build/build_doc.py

# 3. 用 office_convert 把 DOCX 转成 PDF，再提取标题页码
PYTHONPATH=.tools/pylibs $DOC_PY docs/项目书/build/extract_pages.py \
  "docs/项目书/AIOps故障诊断与处置验证平台_技术设计与实现说明.pdf" \
  docs/项目书/build/pages.json

# 4. 填入真实页码并构建
$DOC_PY docs/项目书/build/build_doc.py --pages docs/项目书/build/pages.json
```

说明：概念图由 `make_diagrams_v2.py` 生成，逻辑画布 900px，保证 16cm 宽时正文仍有约 6pt；`extract_pages.py` 依赖 `pypdf`（本仓库放在 `.tools/pylibs`）。本目录保留了已生成的 `pages.json`，正文未增删章节时可直接执行第 4 步。

## 修改建议

- 改正文：编辑 `content/*.md`，重新执行第 4 步；若增删章节，需要重新生成 `pages.json`（目录页码会变）。
- 改概念图：编辑 `build/make_diagrams_v2.py`，重新执行第 1 步。
- 换截图：替换 `assets/shots/` 中同名文件，或在 `make_assets.py` 中调整裁切框。
- 表格与图注：表格不编号；图沿用“图 X-Y”编号，新增图时同步改图注与正文引用。
