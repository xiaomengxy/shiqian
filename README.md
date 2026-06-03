# 拾签

拾签是一个本地 Markdown 笔记库，核心目标是把日常笔记、链接来源和 Bilibili 收藏夹学习内容统一沉淀到一个简洁的笔记工作台里。

当前版本专注三件事：

- 用目录树管理 Markdown 笔记。
- 在浏览器内阅读、编辑、搜索和回收笔记。
- 读取 Bilibili 收藏夹，并把可信的视频字幕或 B 站 AI 摘要整理成笔记。

## 功能概览

- 笔记工作台：左侧目录树和条目，右侧 Markdown 阅读/编辑。
- 目录管理：新建、重命名、删除目录，拖拽目录和笔记调整归属。
- 基础 CRUD：新建、查看、编辑、删除到回收站、恢复、永久删除。
- 回收站保护：笔记页删除入口默认隐藏，可在回收站页打开。
- Markdown 渲染：使用本地 `markdown-it` 静态文件，不依赖外网 CDN。
- 学习页：扫码登录 Bilibili，读取收藏夹，批量生成 Markdown 笔记。
- 后台生成：Bilibili 笔记生成由后端任务执行，切换页面后仍会继续。
- 中断任务：学习页生成中可点击“中断”，已生成的笔记会保留。
- 模型配置：支持 DeepSeek 和 OpenAI，也支持 OpenAI 兼容代理基址。

## Bilibili 笔记策略

拾签不会直接相信字幕或模型输出。

- 标题、UP 主、BVID、链接、视频简介等来源信息只来自 Bilibili 详情接口。
- 字幕和 Bilibili AI 摘要只是候选正文，会先和标题、简介、UP 主关键词做相关性校验。
- 可信内容才会交给 LLM 总结。
- 不可信内容会降级为基础信息笔记，并标记“字幕疑似跑题”。
- 旧版生成但缺少可信度标记的 Bilibili 笔记会被隔离，旧正文不再展示或参与搜索。

这套策略优先保证“不生成误导笔记”，无字幕、错字幕或摘要跑题的视频可能只能生成基础信息。真正深度理解无字幕视频，后续需要接入音频转写或多模态解析。

## 快速开始

### Windows 一键启动

```powershell
.\start.bat
```

脚本会自动创建 `.venv`、复制 `.env.example`、安装依赖并启动服务。

停止服务：

```powershell
.\stop.bat
```

### 手动启动

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

打开：

```text
http://127.0.0.1:8000
```

## 配置

可以在页面右上角“功能 -> 设置”里配置模型，也可以写入 `.env`。

```env
LLM_PROVIDER=deepseek
OPENAI_API_KEY=
OPENAI_MODEL=gpt-5-mini
OPENAI_BASE_URL=https://api.openai.com/v1
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-v4-flash
DATABASE_URL=sqlite:///./bookmarks.db
```

说明：

- 页面保存的设置会写入本地 SQLite，并优先于 `.env`。
- `OPENAI_BASE_URL` 可填写官方基址或兼容代理基址。
- 本地数据库默认是 `bookmarks.db`，不会提交到 Git。

## 常用入口

- `/`：笔记库
- `/learning`：Bilibili 收藏夹学习页
- `/trash`：回收站
- `/settings`：模型和代理配置

## 开发与测试

```powershell
.\.venv\Scripts\python -m pytest
```

当前测试覆盖笔记 CRUD、目录操作、回收站、模型配置、Bilibili 登录/收藏夹读取、后台生成任务、可信度降级和旧笔记隔离。

## 项目结构

```text
app/
  main.py                  FastAPI 路由和后台任务
  models.py                SQLAlchemy 模型
  migrations.py            SQLite 运行时表结构补齐
  services/
    bilibili.py            Bilibili API 客户端
    bilibili_notes.py      字幕可信度判断和笔记生成模板
    settings_store.py      前端设置持久化
  static/
    app.js                 前端交互
    style.css              界面样式
    vendor/markdown-it...  本地 Markdown 渲染器
  templates/               Jinja 页面模板
tests/                     pytest 测试
scripts/                   Windows 启停脚本
```

## 当前限制

- 后台导入任务状态保存在当前 FastAPI 进程内，重启服务后任务进度会丢失，但已写入的笔记不会丢。
- 第一版不下载视频或音频，也不做画面理解。
- Bilibili Cookie 仅保存在本地 SQLite，用于读取你自己的收藏夹。
