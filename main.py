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


@register("jiaran_post", "plugin_dev", "抽然动态", "1.2.0")
class JiaranPostPlugin(Star):
    """嘉然B站动态随机抽取插件"""

    # 嘉然B站 UID
    JIARAN_UID = "672328094"
    # 新版 API（需登录）
    POLYMER_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    # 旧版 API（无需登录，作为降级方案）
    LEGACY_API = "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/space_history"

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
        # 过滤掉可能的空条目
        items = [i for i in items if i is not None]
        if not items:
            yield event.plain_result("❌ 获取嘉然动态失败，请查看日志排查原因~")
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
                data = json.load(f)
            if isinstance(data, list):
                return [i for i in data if i is not None]
            return None
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
        """刷新缓存：有旧缓存时增量拉取，无缓存时全量拉取"""
        async with self._refresh_lock:
            cache = self._load_cache()
            if cache and not self._is_cache_expired():
                return cache

            if cache:
                # 有旧缓存 → 增量刷新
                logger.info(f"[抽然动态] 缓存已过期（{len(cache)}条），开始增量拉取最新动态...")
                existing_ids = {item.get("id_str", "") for item in cache}
                new_items = await self._fetch_new_posts(existing_ids)
                if new_items:
                    # 新动态插到列表最前面
                    merged = new_items + cache
                    self._save_cache(merged)
                    logger.info(f"[抽然动态] 增量刷新完成: 新增 {len(new_items)} 条，总计 {len(merged)} 条")
                    return merged
                else:
                    # 拉不到新的，刷新时间戳继续用旧缓存
                    self._save_cache(cache)
                    logger.info("[抽然动态] 无新动态，继续使用旧缓存")
                    return cache
            else:
                # 无缓存 → 首次全量拉取
                logger.info("[抽然动态] 首次全量拉取嘉然历史动态...")
                items = await self._fetch_all_posts()
                if items:
                    self._save_cache(items)
                    logger.info(f"[抽然动态] 首次全量拉取完成，共 {len(items)} 条动态")
                return items

    # ==================== B站API（分页拉全量） ====================

    def _build_headers(self) -> dict:
        """构建请求头，包含可选的 Cookie"""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": f"https://space.bilibili.com/{self.JIARAN_UID}/dynamic",
            "Origin": "https://space.bilibili.com",
        }
        cookie = self._config.get("bilibili_cookie", "").strip()
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def _fetch_all_posts(self) -> list:
        """分页拉取嘉然全部历史动态，优先新版API，失败降级旧版"""
        items = await self._fetch_with_polymer()
        if not items:
            logger.warning("[抽然动态] 新版API拉取失败，尝试旧版API...")
            items = await self._fetch_with_legacy()
        return items

    async def _fetch_new_posts(self, existing_ids: set) -> list:
        """增量拉取：只拉最新页面，遇到已缓存的帖子就停"""
        new_items = await self._fetch_incremental_polymer(existing_ids)
        if new_items is None:
            logger.warning("[抽然动态] 新版API增量拉取失败，尝试旧版API...")
            new_items = await self._fetch_incremental_legacy(existing_ids)
        return new_items or []

    async def _fetch_incremental_polymer(self, existing_ids: set) -> list | None:
        """新版API增量拉取，遇已缓存帖子停止"""
        new_items = []
        offset = ""
        page = 0

        headers = self._build_headers()

        async with httpx.AsyncClient() as client:
            while True:
                page += 1
                params = {"host_mid": self.JIARAN_UID, "features": "itemOpusStyle"}
                if offset:
                    params["offset"] = offset

                try:
                    resp = await client.get(
                        self.POLYMER_API, params=params, headers=headers, timeout=15
                    )
                    data = resp.json()
                except Exception as e:
                    logger.error(f"[抽然动态] polymer 增量第{page}页失败: {e}")
                    return None if page == 1 else new_items

                if data.get("code") != 0:
                    logger.error(f"[抽然动态] polymer 增量API异常: code={data.get('code')}")
                    return None if page == 1 else new_items

                page_data = data.get("data", {})
                items = page_data.get("items", [])

                for item in items:
                    pid = item.get("id_str", "")
                    if pid in existing_ids:
                        logger.info(f"[抽然动态] polymer 增量: 第{page}页遇到已缓存帖子，停止（新增{len(new_items)}条）")
                        return new_items
                    new_items.append(item)

                has_more = page_data.get("has_more", False)
                offset = page_data.get("offset", "")

                if not has_more:
                    break

                await asyncio.sleep(0.3)

        return new_items

    async def _fetch_incremental_legacy(self, existing_ids: set) -> list | None:
        """旧版API增量拉取，遇已缓存帖子停止"""
        new_items = []
        offset_dynamic_id = "0"
        page = 0

        headers = self._build_headers()

        async with httpx.AsyncClient() as client:
            while True:
                page += 1
                params = {
                    "host_uid": self.JIARAN_UID,
                    "offset_dynamic_id": offset_dynamic_id,
                    "need_top": "0",
                    "platform": "web",
                }

                try:
                    resp = await client.get(
                        self.LEGACY_API, params=params, headers=headers, timeout=15
                    )
                    data = resp.json()
                except Exception as e:
                    logger.error(f"[抽然动态] legacy 增量第{page}页失败: {e}")
                    return None if page == 1 else new_items

                if data.get("code") != 0:
                    return None if page == 1 else new_items

                page_data = data.get("data", {})
                cards = page_data.get("cards", [])
                if not cards:
                    break

                hit_cached = False
                for card in cards:
                    desc = card.get("desc", {})
                    pid = str(desc.get("dynamic_id", ""))
                    if pid in existing_ids:
                        hit_cached = True
                        break
                    try:
                        card_obj = json.loads(card.get("card", "{}"))
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        logger.warning(f"[抽然动态] legacy 增量卡片解析失败，跳过")
                        continue
                    item = {
                        "id_str": pid,
                        "modules": {
                            "module_author": {
                                "pub_ts": int(desc.get("timestamp", 0)),
                            },
                            "module_dynamic": {
                                "desc": {
                                    "text": self._extract_text_from_legacy_card(card_obj),
                                },
                            },
                        },
                    }
                    new_items.append(item)

                if hit_cached:
                    logger.info(f"[抽然动态] legacy 增量: 第{page}页遇到已缓存帖子，停止（新增{len(new_items)}条）")
                    return new_items

                has_more = page_data.get("has_more", 0)
                next_offset = desc.get("dynamic_id_str", "0") if cards else "0"
                offset_dynamic_id = next_offset

                if not has_more:
                    break

                await asyncio.sleep(0.3)

        return new_items

    async def _fetch_with_polymer(self) -> list:
        """使用新版 Polymer API 拉取（需登录态）"""
        all_items = []
        offset = ""
        page = 0
        max_pages = int(self._config.get("max_fetch_pages", 0))

        headers = self._build_headers()

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
                        self.POLYMER_API, params=params, headers=headers, timeout=15
                    )
                    data = resp.json()
                except httpx.TimeoutException:
                    logger.error(f"[抽然动态] polymer 第{page}页请求超时")
                    break
                except Exception as e:
                    logger.error(f"[抽然动态] polymer 第{page}页请求失败: {e}")
                    break

                code = data.get("code")
                if code != 0:
                    logger.error(
                        f"[抽然动态] polymer API返回异常: code={code}, "
                        f"msg={data.get('message')}, HTTP状态={resp.status_code}"
                    )
                    if code == -101 or code == -111:
                        logger.error(
                            "[抽然动态] 需要登录B站账号！请在插件配置中填入 bilibili_cookie "
                            "(浏览器登录B站后从F12→Application→Cookies中复制SESSDATA字段)"
                        )
                    break

                page_data = data.get("data", {})
                items = page_data.get("items", [])
                all_items.extend(items)

                has_more = page_data.get("has_more", False)
                offset = page_data.get("offset", "")

                logger.info(f"[抽然动态] polymer 第{page}页: {len(items)} 条, has_more={has_more}")

                if not has_more:
                    break

                await asyncio.sleep(0.3)

        logger.info(f"[抽然动态] polymer 共 {page} 页，{len(all_items)} 条")
        return all_items

    async def _fetch_with_legacy(self) -> list:
        """使用旧版 API 拉取（无需登录，兜底方案）"""
        all_items = []
        offset_dynamic_id = "0"
        page = 0
        max_pages = int(self._config.get("max_fetch_pages", 0)) or 200

        headers = self._build_headers()

        async with httpx.AsyncClient() as client:
            while True:
                page += 1
                if page > max_pages:
                    break

                params = {
                    "host_uid": self.JIARAN_UID,
                    "offset_dynamic_id": offset_dynamic_id,
                    "need_top": "0",
                    "platform": "web",
                }

                try:
                    resp = await client.get(
                        self.LEGACY_API, params=params, headers=headers, timeout=15
                    )
                    data = resp.json()
                except httpx.TimeoutException:
                    logger.error(f"[抽然动态] legacy 第{page}页请求超时")
                    break
                except Exception as e:
                    logger.error(f"[抽然动态] legacy 第{page}页请求失败: {e}")
                    break

                code = data.get("code")
                if code != 0:
                    logger.error(
                        f"[抽然动态] legacy API返回异常: code={code}, "
                        f"msg={data.get('message')}, HTTP状态={resp.status_code}"
                    )
                    break

                page_data = data.get("data", {})
                cards = page_data.get("cards", [])
                if not cards:
                    break

                # 旧版API卡片格式转换，兼容 _parse_post
                for card in cards:
                    desc = card.get("desc", {})
                    try:
                        card_obj = json.loads(card.get("card", "{}"))
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        logger.warning(f"[抽然动态] legacy 卡片解析失败，跳过")
                        continue
                    item = {
                        "id_str": str(desc.get("dynamic_id", "")),
                        "modules": {
                            "module_author": {
                                "pub_ts": desc.get("timestamp", 0),
                            },
                            "module_dynamic": {
                                "desc": {
                                    "text": self._extract_text_from_legacy_card(card_obj),
                                },
                            },
                        },
                    }
                    all_items.append(item)

                has_more = page_data.get("has_more", 0)
                next_offset = desc.get("dynamic_id_str", "0") if cards else "0"
                offset_dynamic_id = next_offset

                logger.info(f"[抽然动态] legacy 第{page}页: {len(cards)} 条, has_more={has_more}")

                if not has_more:
                    break

                await asyncio.sleep(0.3)

        logger.info(f"[抽然动态] legacy 共 {page} 页，{len(all_items)} 条")
        return all_items

    @staticmethod
    def _extract_text_from_legacy_card(card_obj: dict) -> str:
        """从旧版API卡片中提取文本"""
        item = card_obj.get("item", {})

        # 纯文字动态
        description = item.get("description", "")
        if description:
            return description

        # 带图动态的描述
        desc = card_obj.get("user", {}).get("desc", "")
        if desc:
            return desc

        # 转发动态的原内容
        origin = card_obj.get("origin", "")
        if origin:
            try:
                origin_obj = json.loads(origin) if isinstance(origin, str) else origin
                return origin_obj.get("item", {}).get("description", "")
            except (json.JSONDecodeError, AttributeError):
                pass

        # 小视频/专栏等
        title = item.get("title", "")
        if title:
            return title

        return ""

    @staticmethod
    def _parse_post(item: dict) -> tuple:
        """
        解析单条动态，返回 (文本内容, 发布时间戳, 动态ID)
        处理纯文字、带图、转发等动态类型
        """
        if not isinstance(item, dict):
            return "", 0, ""

        modules = item.get("modules", {})

        # 作者模块 → 发布时间
        author = modules.get("module_author", {})
        pub_ts = int(author.get("pub_ts", 0))

        # 动态 ID
        post_id = item.get("id_str", "")

        # 动态内容模块
        mod_post = modules.get("module_dynamic") or {}
        desc = mod_post.get("desc") or {}
        text = desc.get("text") or ""

        # 如果是转发动态，附加原动态文本
        major = mod_post.get("major") or {}
        orig = major.get("orig")
        if orig:
            orig_desc = (orig.get("desc") or {}).get("text") or ""
            orig_author = (orig.get("module_author") or {}).get("name") or ""
            if orig_desc:
                text = f"{text}\n//@{orig_author}：{orig_desc}"

        # 如果是纯图片动态但无文字
        if not text.strip():
            major_type = major.get("type") or ""
            if major_type == "MAJOR_TYPE_DRAW":
                draw = major.get("draw") or {}
                draw_items = draw.get("items", [])
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
