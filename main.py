"""
抽然动态插件 - 随机抽取嘉然B站动态
用户发送 /抽动态 时，随机抽取一条嘉然的B站日常动态，用LLM以BOT人设评价，并附上原链接和发布时间。
"""
import os
import json
import time
import random
import asyncio

import httpx
from datetime import datetime, timezone, timedelta

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


@register("jiaran_post", "plugin_dev", "抽然动态", "1.1.0")
class JiaranPostPlugin(Star):
    """嘉然B站动态随机抽取插件"""

    # 嘉然B站 UID
    JIARAN_UID = "672328094"
    SPACE_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self._config = config or {}

        # 缓存目录与文件
        self._data_dir = os.path.join("data", "jiaran_post")
        os.makedirs(self._data_dir, exist_ok=True)
        self._cache_path = os.path.join(self._data_dir, "posts_cache.json")
        self._cache_ts_path = os.path.join(self._data_dir, "cache_timestamp.json")

        # 缓存刷新锁，防止并发刷新
        self._refresh_lock = asyncio.Lock()

        # 注册定时刷新任务
        self._register_cron()

    # ==================== 定时任务 ====================

    def _register_cron(self):
        """根据配置注册定时刷新缓存"""
        interval = int(self._config.get("cache_refresh_seconds", 21600))  # 默认6小时
        cron_expr = f"0 */{max(1, interval // 3600)} * * *"
        try:
            self.context.register_task(cron_expr, self._auto_refresh)
        except Exception:
            pass

    async def _auto_refresh(self):
        """定时刷新缓存"""
        await self._refresh_cache()

    # ==================== 指令 ====================

    @filter.command("抽动态")
    async def cmd_draw_post(self, event: AstrMessageEvent):
        """随机抽取一条嘉然B站动态并让LLM评论"""
        # 1. 获取全量动态（优先缓存，缓存过期则刷新）
        items = await self._get_all_posts()
        if not items:
            yield event.plain_result("❌ 获取嘉然动态失败，请稍后重试~")
            return

        # 2. 随机选取一条
        item = random.choice(items)

        # 3. 解析动态内容
        text, pub_ts, post_id = self._parse_post(item)
        if not post_id:
            yield event.plain_result("❌ 解析动态数据出错，请稍后重试~")
            return

        # 4. 构建原动态链接与时间
        link = f"https://t.bilibili.com/{post_id}"
        bj_time = datetime.fromtimestamp(pub_ts, tz=timezone(timedelta(hours=8)))
        time_str = bj_time.strftime("%Y-%m-%d %H:%M:%S")

        # 5. 调用LLM以BOT人设评价
        llm_comment = await self._get_llm_comment(text)

        # 6. 拼接最终消息
        result_parts = ["📢 抽到了一条然然的B站动态！\n"]
        if text.strip():
            result_parts.append(f"「{text.strip()}」\n")
        else:
            result_parts.append("（动态中包含图片/表情，暂无文字描述）\n")

        if llm_comment:
            result_parts.append(f"\n💬 {llm_comment}\n")

        result_parts.append(f"\n🔗 原动态链接：{link}")
        result_parts.append(f"🕐 发布时间：{time_str}")

        yield event.plain_result("\n".join(result_parts))

    # ==================== 缓存管理 ====================

    def _load_cache(self) -> list | None:
        """从本地加载缓存的动态列表"""
        if not os.path.exists(self._cache_path):
            return None
        try:
            with open(self._cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.warning(f"[抽然动态] 缓存文件损坏: {e}")
            return None

    def _save_cache(self, items: list):
        """将动态列表保存到本地缓存"""
        with open(self._cache_path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        with open(self._cache_ts_path, "w", encoding="utf-8") as f:
            json.dump({"updated_at": time.time()}, f)

    def _is_cache_expired(self) -> bool:
        """检查缓存是否过期"""
        if not os.path.exists(self._cache_ts_path):
            return True
        try:
            with open(self._cache_ts_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            elapsed = time.time() - data.get("updated_at", 0)
            max_age = int(self._config.get("cache_max_age_seconds", 21600))  # 默认6小时
            return elapsed > max_age
        except Exception:
            return True

    async def _get_all_posts(self) -> list:
        """获取全量动态，优先使用缓存"""
        cache = self._load_cache()
        if cache and not self._is_cache_expired():
            logger.info(f"[抽然动态] 使用缓存，共 {len(cache)} 条动态")
            return cache

        # 缓存过期或不存在，刷新
        return await self._refresh_cache()

    async def _refresh_cache(self) -> list:
        """刷新缓存：全量拉取并写入本地"""
        async with self._refresh_lock:
            # 双重检查：可能其他协程已刷新
            cache = self._load_cache()
            if cache and not self._is_cache_expired():
                return cache

            logger.info("[抽然动态] 开始全量拉取嘉然历史动态...")
            items = await self._fetch_all_posts()
            if items:
                self._save_cache(items)
                logger.info(f"[抽然动态] 缓存刷新完成，共 {len(items)} 条动态")
            else:
                # 拉取失败，用旧缓存兜底
                stale = self._load_cache()
                if stale:
                    logger.warning("[抽然动态] 拉取失败，使用过期缓存")
                    return stale
            return items

    # ==================== B站API（分页拉全量） ====================

    async def _fetch_all_posts(self) -> list:
        """分页拉取嘉然全部历史动态"""
        all_items = []
        offset = ""
        page = 0
        max_pages = int(self._config.get("max_fetch_pages", 0))  # 0=不限制

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": f"https://space.bilibili.com/{self.JIARAN_UID}/dynamic",
        }

        async with httpx.AsyncClient() as client:
            while True:
                page += 1
                if max_pages > 0 and page > max_pages:
                    logger.info(f"[抽然动态] 达到最大分页限制 {max_pages}，停止拉取")
                    break

                params = {"host_mid": self.JIARAN_UID, "features": "itemOpusStyle"}
                if offset:
                    params["offset"] = offset

                try:
                    resp = await client.get(
                        self.SPACE_API, params=params, headers=headers, timeout=15
                    )
                    data = resp.json()
                except httpx.TimeoutException:
                    logger.error(f"[抽然动态] 第{page}页请求超时，停止")
                    break
                except Exception as e:
                    logger.error(f"[抽然动态] 第{page}页请求失败: {e}，停止")
                    break

                if data.get("code") != 0:
                    logger.error(f"[抽然动态] API返回异常: code={data.get('code')}, msg={data.get('message')}")
                    break

                page_data = data.get("data", {})
                items = page_data.get("items", [])
                all_items.extend(items)

                has_more = page_data.get("has_more", False)
                offset = page_data.get("offset", "")

                logger.info(f"[抽然动态] 第{page}页: {len(items)} 条, has_more={has_more}")

                if not has_more:
                    break

                # B站API限流保护
                await asyncio.sleep(0.3)

        logger.info(f"[抽然动态] 共拉取 {page} 页，总计 {len(all_items)} 条动态")
        return all_items

    @staticmethod
    def _parse_post(item: dict) -> tuple:
        """
        解析单条动态，返回 (文本内容, 发布时间戳, 动态ID)
        处理纯文字、带图、转发等动态类型
        """
        modules = item.get("modules", {})

        # 作者模块 → 发布时间
        author = modules.get("module_author", {})
        pub_ts = author.get("pub_ts", 0)

        # 动态 ID
        post_id = item.get("id_str", "")

        # 动态内容模块
        mod_post = modules.get("module_dynamic", {})
        desc = mod_post.get("desc", {})
        text = desc.get("text", "")

        # 如果是转发动态，附加原动态文本
        orig = mod_post.get("major", {}).get("orig")
        if orig:
            orig_desc = orig.get("desc", {}).get("text", "")
            orig_author = orig.get("module_author", {}).get("name", "")
            if orig_desc:
                text = f"{text}\n//@{orig_author}：{orig_desc}"

        # 如果是纯图片动态但无文字
        if not text.strip():
            major_type = mod_post.get("major", {}).get("type", "")
            if major_type == "MAJOR_TYPE_DRAW":
                draw_items = mod_post.get("major", {}).get("draw", {}).get("items", [])
                text = f"[分享了{len(draw_items)}张图片]"

        return text, pub_ts, post_id

    # ==================== LLM 评论 ====================

    async def _get_llm_comment(self, post_text: str) -> str:
        """调用LLM以BOT人设评价动态"""
        prompt = (
            f"下面是一条来自虚拟偶像「嘉然今天吃什么」在B站发布的最新动态。"
            f"请你以你的人物设定，从粉丝视角对这条动态发表一段简短的评论，"
            f"要求语气生动活泼、有真实粉丝的情感，不超过150字。"
            f"直接输出评论即可，不要加任何前缀。\n\n"
            f"动态内容：\n{post_text if post_text.strip() else '（无文字内容，可能是纯图片动态）'}"
        )

        try:
            provider_id = self._config.get("llm_provider_id_override") or None
            resp = await self.context.llm_generate(
                prompt=prompt,
                chat_provider_id=provider_id,
            )
            if resp and hasattr(resp, "completion_text"):
                return resp.completion_text.strip()
            elif isinstance(resp, str):
                return resp.strip()
        except Exception as e:
            logger.error(f"[抽然动态] LLM调用失败: {e}")

        return ""
