# 抽然动态

AstrBot 插件 —— 随机抽取嘉然全量 B站历史动态，调用 LLM 以 BOT 人设评价，并附上原动态链接与发布时间。

## 指令

| 指令 | 权限 | 说明 |
|------|------|------|
| `/抽动态` | 全员 | 从全量历史动态中随机抽取一条，发送正文+图片+LLM评论+链接+时间 |
| `/更新动态` | 全员 | 手动增量拉取最新动态 |
| `/强制刷新动态` | 管理员 | 删除缓存，全量重新拉取所有历史动态 |

## 功能

- 支持纯文字、带图、转发、表情等所有B站动态类型
- 自动下载并发送动态中的图片
- 文本优先从 `desc.text` 提取，fallback 到 `rich_text_nodes` 解析（含表情）
- 首次全量拉取，后续增量更新（遇已缓存帖子即停）
- 定时自动增量刷新

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `bilibili_cookie` | string | 空 | B站 Cookie（SESSDATA），新版API必需 |
| `llm_provider_id_override` | string | 空 | LLM提供商ID，留空用默认 |
| `cache_max_age_seconds` | int | 21600 | 缓存有效期（秒），默认 6 小时 |
| `max_fetch_pages` | int | 0 | 拉取页数上限，0 不限制 |

## 部署

1. 将本文件夹放入 AstrBot 插件目录
2. 安装依赖：`pip install httpx>=0.25.0`
3. 配置 B站 Cookie：浏览器登录B站 → F12 → Application → Cookies → 复制 `SESSDATA` 填入 `bilibili_cookie`
4. 重启 AstrBot
