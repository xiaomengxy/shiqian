# 拾签

拾签是一个本地 FastAPI Web 应用，用来拾起链接、词、短句和段落，自动生成名称、简介、标签、关键词和多级目录建议，并保存到 SQLite。

## Quick Start

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python -m uvicorn app.main:app --reload
```

打开 http://127.0.0.1:8000。

如果没有配置 `OPENAI_API_KEY` 或 `DEEPSEEK_API_KEY`，拾签会使用本地规则生成可编辑建议。

## 功能

- 混合输入：URL 会单独解析，非 URL 文本会按空行拆成词条或文本收藏。
- 自动整理：为内容生成名称、简介、标签、关键词和多级目录建议。
- 链接解析：抓取公开网页/视频元信息后生成收藏建议。
- 文本收藏：直接整理原文，适度补足便于检索和回看的说明。
- 收藏列表：默认显示名称、目录、标签、关键词，展开后查看简介和原始内容。

## 配置

可以打开 `/settings` 在前端直接配置默认 provider、模型和 API key；这些值会保存到本地 SQLite，优先于 `.env` 生效。

- `LLM_PROVIDER`: `openai` 或 `deepseek`
- `OPENAI_API_KEY` / `OPENAI_MODEL`
- `DEEPSEEK_API_KEY` / `DEEPSEEK_MODEL`
- `DATABASE_URL`: 默认 `sqlite:///./bookmarks.db`

## 测试

```powershell
.\.venv\Scripts\python -m pytest
```
