# 拾签

拾签是一个本地 FastAPI Markdown 笔记库。正文以 Markdown 存储，在浏览器里渲染成阅读视图；当前主流程只保留简单的增删改查、搜索、回收站和模型设置。

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

打开 http://127.0.0.1:8000。

## 功能

- Markdown 笔记：标题、正文、轻量目录、可选来源链接。
- 浏览器渲染：本地静态 Markdown 渲染器，无需外网 CDN。
- 简单 CRUD：新建、查看、编辑、删除。
- 回收站：软删除、恢复、永久删除。
- 搜索：匹配标题、正文和来源链接。
- 目录：使用 `技术/AI` 这样的路径形成左侧层级树，不包含拖拽和批量管理。
- 预留来源：`manual`、`url`、`bilibili`，Bilibili 接入后续再实现。

## 配置

可以打开 `/settings` 在前端配置默认 provider、模型和 API key；这些值会保存到本地 SQLite，优先于 `.env` 生效。当前版本保留这些设置，供后续文本、链接、视频解析使用。

- `LLM_PROVIDER`: `openai` 或 `deepseek`
- `OPENAI_API_KEY` / `OPENAI_MODEL`
- `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL`
- `DATABASE_URL`: 默认 `sqlite:///./bookmarks.db`

## 测试

```powershell
.\.venv\Scripts\python -m pytest
```
