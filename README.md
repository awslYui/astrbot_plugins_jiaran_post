# 抽然动态

AstrBot 插件 —— 随机抽取嘉然全量 B站历史动态，调用 LLM 以 BOT 人设评价，并附上原动态链接与发布时间。

## 指令

| 指令 | 说明 |
|------|------|
| `/抽动态` | 从嘉然全量历史动态中随机抽取一条并发送 |

## 快速开始

1. 将本文件夹放入 AstrBot 插件目录
2. 安装依赖：`pip install httpx>=0.25.0`
3. **配置 B站 Cookie**（重要）：
   - 用浏览器登录 B站
   - F12 → Application → Cookies → 复制 `SESSDATA` 的值
   - 在插件配置中填入 `bilibili_cookie`
4. 重启 AstrBot

> 即使不填 Cookie，插件也会自动降级到旧版 API 拉取，但稳定性较差。

## 配置

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `bilibili_cookie` | string | 空 | B站 Cookie，新版API必需 |
| `llm_provider_id_override` | string | 空 | LLM提供商ID，留空用默认 |
| `cache_max_age_seconds` | int | 21600 | 缓存有效期（秒），默认 6 小时 |
| `max_fetch_pages` | int | 0 | 拉取页数上限，0 不限制 |
