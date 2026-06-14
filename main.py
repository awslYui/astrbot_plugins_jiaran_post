"""
抽然动态插件 - 随机抽取嘉然B站动态
用户发送 /抽动态 时，随机抽取一条嘉然的B站日常动态，用LLM以BOT人设评价，并附上原链接和发布时间。
"""
import os
import json
import time
import random
import re
import asyncio

import httpx
from datetime import datetime, timezone, timedelta

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger


@register("jiaran_post", "plugin_dev", "抽然动态", "1.6.0")
class JiaranPostPlugin(Star):
    """嘉然B站动态随机抽取插件"""

    JIARAN_UID = "672328094"
    POLYMER_API = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    LEGACY_API = "https://api.vc.bilibili.com/dynamic_svr/v1/dynamic_svr/space_history"
    SKIP_TYPES = {"MAJOR_TYPE_ARCHIVE", "MAJOR_TYPE_LIVE_RCMD"}

    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self._config = config or {}

        self._data_dir = os.path.join("data", "jiaran_post")
        os.makedirs(self._data_dir, exist_ok=True)
        self._cache_path = os.path.join(self._data_dir, "posts_cache.json")
        self._cache_ts_path = os.path.join(self._data_dir, "cache_timestamp.json")
        self._img_dir = os.path.join(self._data_dir, "images")
        os.makedirs(self._img_dir, exist_ok=True)
        self._fetch_state_path = os.path.join(self._data_dir, "fetch_state.json")
        self._refresh_lock = asyncio.Lock()
        self._register_cron()

    # ==================== 定时任务 ====================

    def _register_cron(self):
        interval = int(self._config.get("cache_refresh_seconds", 21600))
        cron_expr = f"0 */{max(1, interval // 3600)} * * *"
        try:
            self.context.register_task(cron_expr, self._auto_refresh)
        except Exception:
            pass

    async def _auto_refresh(self):
        await self._refresh_cache()

    # ==================== 日期解析 ====================

    @staticmethod
    def _parse_date_input(raw: str):
        """
        尝试多种格式解析日期输入 → (year, month, day) 或 (None, err_msg)
        支持格式：2024-7-24, 2024/07/24, 2024.7.24, 20240724,
                 7月24日, 7月24号, 2024年7月24日, 24/7/2024
        """
        raw = raw.strip()
        if not raw:
            return None, "输入为空"

        patterns = [
            # 2024-07-24, 2024/07/24, 2024.07.24
            (r'^(\d{4})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})$',
             lambda m: (int(m[1]), int(m[2]), int(m[3]))),
            # 2024年7月24日 / 号
            (r'^(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?$',
             lambda m: (int(m[1]), int(m[2]), int(m[3]))),
            # 7月24日 / 号 (补当前年份)
            (r'^(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]$',
             lambda m: (datetime.now().year, int(m[1]), int(m[2]))),
            # 20240724
            (r'^(\d{4})(\d{2})(\d{2})$',
             lambda m: (int(m[1]), int(m[2]), int(m[3]))),
            # 24/7/2024
            (r'^(\d{1,2})\s*/\s*(\d{1,2})\s*/\s*(\d{4})$',
             lambda m: (int(m[3]), int(m[2]), int(m[1]))),
        ]

        for pattern, extract in patterns:
            m = re.match(pattern, raw)
            if m:
                y, mo, d = extract(m)
                if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                    try:
                        datetime(y, mo, d)
                        return (y, mo, d), None
                    except ValueError:
                        return None, f"「{raw}」日期不存在（如2月30日）"
                else:
                    return None, f"「{raw}」日期数值超出范围"

        # 检查是否像日期但无法解析
        if re.search(r'[\d]{3,}|[年月日号]', raw):
            return None, (
                f"「{raw}」看起来像日期但格式不对。\n"
                f"支持格式：2024-7-24 / 2024/07/24 / 20240724 / 7月24日 / 2024年7月24日"
            )

        return None, None  # 不像是日期，当作普通关键词

    # ==================== 指令 ====================

    @filter.command("抽动态")

    async def cmd_draw_post(self, event: AstrMessageEvent):
        """随机抽取一条嘉然B站动态并让LLM评论"""
        items = await self._get_all_posts()
        items = [i for i in items if i is not None]
        if not items:
            yield event.plain_result("❌ 获取嘉然动态失败，请查看日志排查原因~")
            return

        # 过滤掉视频投稿和直播动态
        candidates = [i for i in items if self._get_post_type(i) not in self.SKIP_TYPES]
        if not candidates:
            yield event.plain_result("❌ 没有可用的日常动态~")
            return

        item = random.choice(candidates)
        async for result in self._render_one_post(event, item, "📢 抽到了一条然然的B站动态！\n"):
            yield result

    @filter.command("搜动态")
    async def cmd_search_post(self, event: AstrMessageEvent):
        """按关键词或日期搜索嘉然动态并让LLM评论"""
        raw_text = event.message_str.strip()
        # 提取参数（去掉指令前缀 "/搜动态"）
        param = re.sub(r'^[/\s]*搜动态\s*', '', raw_text).strip()
        if not param:
            yield event.plain_result(
                "❌ 请输入关键词或日期，例如：\n"
                "/搜动态 晚安\n"
                "/搜动态 2024-07-24"
            )
            return

        # 尝试日期解析
        (date_tuple, date_err) = self._parse_date_input(param)
        if date_err:
            yield event.plain_result(date_err)
            return

        items = await self._get_all_posts()
        items = [i for i in items if i is not None]
        candidates = [i for i in items if self._get_post_type(i) not in self.SKIP_TYPES]
        if not candidates:
            yield event.plain_result("❌ 没有可用的日常动态~")
            return

        if date_tuple:
            # --- 日期搜索 ---
            y, mo, d = date_tuple
            tz_cn = timezone(timedelta(hours=8))
            start_ts = int(datetime(y, mo, d, tzinfo=tz_cn).timestamp())
            end_ts = int(datetime(y, mo, d, 23, 59, 59, tzinfo=tz_cn).timestamp())

            matched = []
            for item in candidates:
                ts = self._get_post_timestamp(item)
                if start_ts <= ts <= end_ts:
                    matched.append(item)

            if not matched:
                date_str = f"{y}年{mo}月{d}日"
                yield event.plain_result(f"❌ 没有找到 {date_str} 的动态~")
                return

            item = random.choice(matched)
            date_str = f"{y}年{mo}月{d}日"
            async for result in self._render_one_post(
                event, item,
                f"📢 抽到一条 {date_str} 的然然动态！（当天共{len(matched)}条）\n"
            ):
                yield result
        else:
            # --- 关键词搜索 ---
            matched = []
            for item in candidates:
                post_text = self._extract_post_text_fast(item)
                if param.lower() in post_text.lower():
                    matched.append(item)

            if not matched:
                yield event.plain_result(f"❌ 没有找到包含「{param}」的动态~")
                return

            item = random.choice(matched)
            async for result in self._render_one_post(
                event, item,
                f"📢 搜到一条含「{param}」的然然动态！（共{len(matched)}条匹配）\n"
            ):
                yield result

    async def _render_one_post(self, event: AstrMessageEvent, item: dict, header: str):
        """渲染并发送单条动态（正文+图片+LLM评论+链接+时间）"""
        text, pub_ts, post_id, img_urls = self._parse_post(item)
        if not post_id:
            yield event.plain_result("❌ 解析动态数据出错，请稍后重试~")
            return

        # 根据动态类型生成正确链接
        post_type = self._get_post_type(item)
        if post_type == "MAJOR_TYPE_OPUS":
            link = f"https://www.bilibili.com/opus/{post_id}"
        else:
            link = f"https://t.bilibili.com/{post_id}"
        bj_time = datetime.fromtimestamp(pub_ts, tz=timezone(timedelta(hours=8)))
        time_str = bj_time.strftime("%Y-%m-%d %H:%M:%S")

        # 发送图片
        if img_urls:
            downloaded = await self._download_images(img_urls)
            for img_path in downloaded:
                yield event.image_result(img_path)

        # LLM 评论（传入发布时间）
        llm_comment = await self._get_llm_comment(text, time_str)

        # 拼接正文
        parts = [header]
        if text.strip():
            parts.append(f"「{text.strip()}」")
        else:
            parts.append("（纯图片动态，没有文字哦~）")

        if llm_comment:
            parts.append(f"\n💬 {llm_comment}")

        parts.append(
            f"\n🔗 原动态链接：{link}\n"
            f"🕐 发布时间：{time_str}"
        )

        yield event.plain_result("\n".join(parts))

    @staticmethod
    def _extract_post_text_fast(item: dict) -> str:
        """快速提取动态文本（用于关键词匹配，不做完整解析）"""
        if not isinstance(item, dict):
            return ""
        modules = item.get("modules") or {}
        mod_post = modules.get("module_dynamic") or {}
        # desc.text
        desc = mod_post.get("desc") or {}
        text = desc.get("text") or ""
        # rich_text_nodes
        if not text:
            text = JiaranPostPlugin._extract_text_from_summary(desc)
        # opus
        major = mod_post.get("major") or {}
        if major.get("type") == "MAJOR_TYPE_OPUS" and not text:
            opus = major.get("opus") or {}
            text = JiaranPostPlugin._extract_text_from_summary(opus.get("summary") or {})
        return text

    @staticmethod
    def _get_post_timestamp(item: dict) -> int:
        """快速获取动态时间戳（不做完整解析）"""
        if not isinstance(item, dict):
            return 0
        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        return int(author.get("pub_ts", 0))

    @filter.command("更新动态")
    async def cmd_update(self, event: AstrMessageEvent):
        """手动增量更新缓存（全员可用）"""
        yield event.plain_result("🔄 正在检查嘉然最新动态...")
        cache = self._load_cache()
        if not cache:
            yield event.plain_result("📡 无本地缓存，正在首次全量拉取，请稍候...")
            items = await self._force_refresh()
            if items:
                yield event.plain_result(f"✅ 首次拉取完成，共 {len(items)} 条动态")
            else:
                yield event.plain_result("❌ 拉取失败")
            return

        existing_ids = {item.get("id_str", "") for item in cache}
        new_items = await self._fetch_new_posts(existing_ids)
        if new_items:
            merged = new_items + cache
            self._save_cache(merged)
            yield event.plain_result(f"✅ 更新完成，新增 {len(new_items)} 条动态（总计 {len(merged)} 条）")
        else:
            self._save_cache(cache)  # 刷新时间戳
            yield event.plain_result("✅ 已是最新，没有新动态~")

    @filter.command("强制刷新动态")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_force_refresh(self, event: AstrMessageEvent):
        """强制全量重新拉取（仅管理员）"""
        yield event.plain_result("� 正在全量重新拉取嘉然历史动态，这可能需要一些时间...")
        items = await self._force_refresh()
        if items:
            yield event.plain_result(f"✅ 全量刷新完成，共 {len(items)} 条动态")
        else:
            yield event.plain_result("❌ 全量拉取失败，请查看日志")

    async def _force_refresh(self) -> list:
        """强制全量拉取并覆盖缓存"""
        async with self._refresh_lock:
            if os.path.exists(self._cache_path):
                os.remove(self._cache_path)
            if os.path.exists(self._cache_ts_path):
                os.remove(self._cache_ts_path)
            items = await self._fetch_all_posts()
            if items:
                self._save_cache(items)
            return items

    # ==================== 缓存管理 ====================

    def _load_cache(self) -> list | None:
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
        with open(self._cache_path, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        with open(self._cache_ts_path, "w", encoding="utf-8") as f:
            json.dump({"updated_at": time.time()}, f)

    def _is_cache_expired(self) -> bool:
        if not os.path.exists(self._cache_ts_path):
            return True
        try:
            with open(self._cache_ts_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            elapsed = time.time() - data.get("updated_at", 0)
            max_age = int(self._config.get("cache_max_age_seconds", 21600))
            return elapsed > max_age
        except Exception:
            return True

    async def _get_all_posts(self) -> list:
        cache = self._load_cache()
        if cache and not self._is_cache_expired():
            logger.info(f"[抽然动态] 使用缓存，共 {len(cache)} 条动态")
            return cache
        return await self._refresh_cache()

    async def _refresh_cache(self) -> list:
        """有旧缓存 → 增量，无缓存 → 全量"""
        async with self._refresh_lock:
            cache = self._load_cache()
            if cache and not self._is_cache_expired():
                return cache

            if cache:
                logger.info(f"[抽然动态] 增量拉取（缓存 {len(cache)} 条）...")
                existing_ids = {item.get("id_str", "") for item in cache}
                new_items = await self._fetch_new_posts(existing_ids)
                if new_items:
                    merged = new_items + cache
                    self._save_cache(merged)
                    logger.info(f"[抽然动态] 增量完成: +{len(new_items)}，共 {len(merged)} 条")
                    return merged
                else:
                    self._save_cache(cache)
                    logger.info("[抽然动态] 无新动态")
                    return cache
            else:
                logger.info("[抽然动态] 首次全量拉取...")
                items = await self._fetch_all_posts()
                if items:
                    self._save_cache(items)
                    logger.info(f"[抽然动态] 全量完成: {len(items)} 条")
                return items

    # ==================== B站API ====================

    def _load_fetch_state(self) -> dict:
        """加载断点续拉状态"""
        if not os.path.exists(self._fetch_state_path):
            return {}
        try:
            with open(self._fetch_state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_fetch_state(self, state: dict):
        """保存断点续拉状态"""
        try:
            with open(self._fetch_state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[抽然动态] 保存断点状态失败: {e}")

    def _build_headers(self) -> dict:
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
        prev_state = self._load_fetch_state()
        all_polymer = []
        last_polymer_offset = ""

        # 第一轮：polymer 正常拉取
        polymer_items, last_polymer_offset = await self._fetch_with_polymer()
        if not polymer_items:
            logger.warning("[抽然动态] polymer 空，降级纯 legacy...")
            legacy_items, last_legacy_did = await self._fetch_with_legacy()
            # 保存 legacy 断点
            if last_legacy_did:
                self._save_fetch_state({
                    "polymer_offset": last_polymer_offset,
                    "legacy_dynamic_id": last_legacy_did,
                })
            return legacy_items

        all_polymer = polymer_items
        logger.info(f"[抽然动态] polymer 拉完 {len(polymer_items)} 条")

        # 第二轮：从上轮断点续拉 polymer 旧数据
        prev_polymer_offset = prev_state.get("polymer_offset", "")
        if prev_polymer_offset:
            logger.info(f"[抽然动态] polymer 续拉：从 {prev_polymer_offset} 继续...")
            continuation, new_last_offset = await self._fetch_with_polymer(start_offset=prev_polymer_offset)
            if continuation:
                existing_ids = {i.get("id_str") for i in all_polymer}
                new = 0
                for item in continuation:
                    if item.get("id_str") not in existing_ids:
                        all_polymer.append(item)
                        existing_ids.add(item["id_str"])
                        new += 1
                logger.info(f"[抽然动态] polymer 续拉 +{new} 条")
                last_polymer_offset = new_last_offset

        # 第三轮：legacy 补拉旧动态
        logger.info("[抽然动态] legacy 补拉旧动态...")
        legacy_items, last_legacy_did = await self._fetch_with_legacy()
        if legacy_items:
            existing_ids = {i.get("id_str") for i in all_polymer}
            new_count = 0
            for li in legacy_items:
                if li.get("id_str") not in existing_ids:
                    all_polymer.append(li)
                    existing_ids.add(li["id_str"])
                    new_count += 1
            logger.info(f"[抽然动态] legacy 补拉 +{new_count} 条（合并后 {len(all_polymer)} 条）")

        # 第四轮：从 legacy 断点续拉
        prev_legacy_did = prev_state.get("legacy_dynamic_id", "")
        if prev_legacy_did:
            logger.info(f"[抽然动态] legacy 续拉：从 {prev_legacy_did} 继续...")
            continuation, new_legacy_did = await self._fetch_with_legacy(start_dynamic_id=prev_legacy_did)
            if continuation:
                existing_ids = {i.get("id_str") for i in all_polymer}
                new = 0
                for item in continuation:
                    if item.get("id_str") not in existing_ids:
                        all_polymer.append(item)
                        existing_ids.add(item["id_str"])
                        new += 1
                logger.info(f"[抽然动态] legacy 续拉 +{new} 条")
                last_legacy_did = new_legacy_did

        # 保存断点
        self._save_fetch_state({
            "polymer_offset": last_polymer_offset,
            "legacy_dynamic_id": last_legacy_did,
        })

        return all_polymer

    async def _fetch_new_posts(self, existing_ids: set) -> list:
        new_items = await self._fetch_incremental_polymer(existing_ids)
        if new_items is None:
            logger.warning("[抽然动态] polymer 增量失败，降级 legacy...")
            new_items = await self._fetch_incremental_legacy(existing_ids)
        return new_items or []

    async def _fetch_incremental_polymer(self, existing_ids: set) -> list | None:
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
                    resp = await client.get(self.POLYMER_API, params=params, headers=headers, timeout=15)
                    data = resp.json()
                except Exception as e:
                    logger.error(f"[抽然动态] polymer 增量第{page}页: {e}")
                    return None if page == 1 else new_items
                if data.get("code") != 0:
                    return None if page == 1 else new_items
                page_data = data.get("data", {})
                items = page_data.get("items", [])
                for item in items:
                    pid = item.get("id_str", "")
                    if pid in existing_ids:
                        logger.info(f"[抽然动态] polymer 增量: 遇已缓存，+{len(new_items)}条")
                        return new_items
                    new_items.append(item)
                has_more = page_data.get("has_more", False)
                offset = page_data.get("offset", "")
                if not has_more:
                    break
                await asyncio.sleep(0.8)
        return new_items

    async def _fetch_incremental_legacy(self, existing_ids: set) -> list | None:
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
                    resp = await client.get(self.LEGACY_API, params=params, headers=headers, timeout=15)
                    data = resp.json()
                except Exception as e:
                    logger.error(f"[抽然动态] legacy 增量第{page}页: {e}")
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
                        continue
                    legacy_type = int(desc.get("type", 0))
                    new_items.append(self._build_item_from_legacy(desc, card_obj, pid, legacy_type))
                if hit_cached:
                    logger.info(f"[抽然动态] legacy 增量: 遇已缓存，+{len(new_items)}条")
                    return new_items
                has_more = page_data.get("has_more", 0)
                next_offset = desc.get("dynamic_id_str", "0") if cards else "0"
                offset_dynamic_id = next_offset
                if not has_more:
                    break
                await asyncio.sleep(0.8)
        return new_items

    async def _fetch_with_polymer(self, start_offset: str = "") -> tuple:
        """polymer 全量拉取 → (items, last_offset)"""
        all_items = []
        offset = start_offset
        page = 0
        last_good_offset = start_offset
        max_pages = int(self._config.get("max_fetch_pages", 0))
        headers = self._build_headers()

        async with httpx.AsyncClient() as client:
            while True:
                page += 1
                if max_pages > 0 and page > max_pages:
                    break
                params = {"host_mid": self.JIARAN_UID, "features": "itemOpusStyle"}
                if offset:
                    params["offset"] = offset
                try:
                    resp = await client.get(self.POLYMER_API, params=params, headers=headers, timeout=15)
                    data = resp.json()
                except httpx.TimeoutException:
                    logger.error(f"[抽然动态] polymer 第{page}页超时")
                    break
                except Exception:
                    logger.info(f"[抽然动态] polymer 第{page}页无数据，翻页终止")
                    break
                if data.get("code") != 0:
                    code = data.get("code")
                    if code in (-352, -799):
                        logger.warning(f"[抽然动态] polymer 被限流 (code={code})，已保存断点，下次继续")
                    elif code in (-101, -111):
                        logger.error("[抽然动态] 需要登录！请在配置中填入 bilibili_cookie")
                    else:
                        logger.error(f"[抽然动态] polymer: code={code}, msg={data.get('message')}")
                    break
                page_data = data.get("data", {})
                items = page_data.get("items", [])
                all_items.extend(items)
                logger.info(f"[抽然动态] polymer 第{page}页: {len(items)} 条")
                has_more = page_data.get("has_more", False)
                offset = page_data.get("offset", "")
                if offset:
                    last_good_offset = offset
                if not has_more:
                    break
                await asyncio.sleep(0.8)
        label = "续拉" if start_offset else ""
        logger.info(f"[抽然动态] polymer{label} 共 {page} 页，{len(all_items)} 条")
        if all_items:
            last = all_items[-1]
            ts = JiaranPostPlugin._get_post_timestamp(last)
            bj = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") if ts else "?"
            txt = JiaranPostPlugin._extract_post_text_fast(last)[:60]
            logger.info(f"[抽然动态] polymer{label} 最旧动态: [{bj}] {txt}")
        return all_items, last_good_offset

    async def _fetch_with_legacy(self, start_dynamic_id: str = "") -> tuple:
        """legacy 全量拉取 → (items, last_dynamic_id)"""
        all_items = []
        offset_dynamic_id = start_dynamic_id or "0"
        page = 0
        last_good_id = offset_dynamic_id
        max_pages = int(self._config.get("max_fetch_pages", 0))
        if max_pages <= 0:
            max_pages = 500
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
                    resp = await client.get(self.LEGACY_API, params=params, headers=headers, timeout=15)
                    data = resp.json()
                except httpx.TimeoutException:
                    logger.error(f"[抽然动态] legacy 第{page}页超时")
                    break
                except Exception:
                    sc = resp.status_code if 'resp' in dir() else "?"
                    logger.info(f"[抽然动态] legacy 第{page}页无响应 (HTTP {sc})，翻页终止")
                    break
                if data.get("code") != 0:
                    logger.error(f"[抽然动态] legacy: code={data.get('code')}, msg={data.get('message')}")
                    break
                page_data = data.get("data", {})
                cards = page_data.get("cards", [])
                if not cards:
                    break
                for card in cards:
                    desc = card.get("desc", {})
                    pid = str(desc.get("dynamic_id", ""))
                    legacy_type = int(desc.get("type", 0))
                    try:
                        card_obj = json.loads(card.get("card", "{}"))
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        continue
                    all_items.append(self._build_item_from_legacy(desc, card_obj, pid, legacy_type))
                logger.info(f"[抽然动态] legacy 第{page}页: {len(cards)} 条")
                has_more = page_data.get("has_more", 0)
                next_offset = desc.get("dynamic_id_str", "0") if cards else "0"
                offset_dynamic_id = next_offset
                if offset_dynamic_id and offset_dynamic_id != "0":
                    last_good_id = offset_dynamic_id
                if not has_more:
                    break
                await asyncio.sleep(0.8)
        label = "续拉" if start_dynamic_id else ""
        logger.info(f"[抽然动态] legacy{label} 共 {page} 页，{len(all_items)} 条")
        if all_items:
            last = all_items[-1]
            ts = JiaranPostPlugin._get_post_timestamp(last)
            bj = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") if ts else "?"
            txt = JiaranPostPlugin._extract_post_text_fast(last)[:60]
            logger.info(f"[抽然动态] legacy{label} 最旧动态: [{bj}] {txt}")
        return all_items, last_good_id

    def _build_item_from_legacy(self, desc: dict, card_obj: dict, pid: str, legacy_type: int = 0) -> dict:
        """统一的旧版卡片 → 新版格式转换"""
        text, img_urls = self._extract_from_legacy_card(card_obj)
        # 旧版 type → polymer major_type 映射
        type_map = {
            2: "MAJOR_TYPE_DRAW",
            8: "MAJOR_TYPE_ARCHIVE",
            64: "MAJOR_TYPE_ARTICLE",
        }
        major_type = type_map.get(legacy_type, "")
        return {
            "id_str": pid,
            "modules": {
                "module_author": {"pub_ts": int(desc.get("timestamp", 0))},
                "module_dynamic": {
                    "desc": {"text": text},
                    "major": {"type": major_type} if major_type else {},
                    "_img_urls": img_urls,
                },
            },
        }

    @staticmethod
    def _extract_from_legacy_card(card_obj: dict) -> tuple:
        """从旧版卡片提取 (文本, 图片URL列表)"""
        item = card_obj.get("item") or {}
        img_urls = []

        # 文本
        text = (item.get("description") or
                (card_obj.get("user") or {}).get("desc") or
                item.get("content") or
                item.get("title") or "")

        # 转发原内容
        origin = card_obj.get("origin", "")
        if origin and isinstance(origin, str):
            try:
                origin_obj = json.loads(origin)
                orig_text = (origin_obj.get("item", {}) or {}).get("description", "")
                if orig_text:
                    text = f"{text}\n//转发：{orig_text}" if text else f"//转发：{orig_text}"
            except (json.JSONDecodeError, AttributeError):
                pass

        # 图片
        pictures = item.get("pictures", [])
        for pic in pictures:
            img_src = pic.get("img_src", "")
            if img_src:
                img_urls.append(img_src)

        return text or "", img_urls

    # ==================== 动态解析 ====================

    @staticmethod
    def _get_post_type(item: dict) -> str:
        """获取动态类型（polymer major_type），用于过滤"""
        if not isinstance(item, dict):
            return ""
        modules = item.get("modules") or {}
        mod_post = modules.get("module_dynamic") or {}
        major = mod_post.get("major") or {}
        return major.get("type") or ""

    @staticmethod
    def _extract_text_from_summary(summary: dict) -> str:
        """从 summary 对象（含 text + rich_text_nodes）提取完整文本"""
        text = summary.get("text") or ""
        if text:
            return text
        nodes = summary.get("rich_text_nodes", [])
        parts = []
        for node in nodes:
            ntype = node.get("type", "")
            if ntype == "RICH_TEXT_NODE_TYPE_TEXT":
                parts.append(node.get("text", ""))
            elif ntype == "RICH_TEXT_NODE_TYPE_EMOJI":
                emoji = node.get("emoji") or {}
                parts.append(emoji.get("text", "[表情]"))
            elif ntype == "RICH_TEXT_NODE_TYPE_AT":
                parts.append(f"@{node.get('text', node.get('rid', ''))}")
            elif ntype == "RICH_TEXT_NODE_TYPE_WEB":
                parts.append(f"[链接：{node.get('text', '')}]")
            elif ntype == "RICH_TEXT_NODE_TYPE_BV":
                parts.append(f"[BV视频]")
            elif ntype == "RICH_TEXT_NODE_TYPE_TOPIC":
                parts.append(f"#{node.get('text', '')}#")
            else:
                parts.append(node.get("text", ""))
        return "".join(parts)

    @staticmethod
    def _parse_post(item: dict) -> tuple:
        """
        解析单条动态 → (文本, 时间戳, 动态ID, 图片URL列表)
        支持 polymer 和 legacy 两种格式
        """
        if not isinstance(item, dict):
            return "", 0, "", []

        modules = item.get("modules") or {}

        author = modules.get("module_author") or {}
        pub_ts = int(author.get("pub_ts", 0))
        post_id = item.get("id_str", "")

        mod_post = modules.get("module_dynamic") or {}

        # 图片（legacy 格式直接存在 _img_urls 中）
        img_urls = list(mod_post.get("_img_urls", []))

        # 文本：优先 desc.text，其次 rich_text_nodes
        desc = mod_post.get("desc") or {}
        text = desc.get("text") or ""

        if not text.strip():
            text = JiaranPostPlugin._extract_text_from_summary(desc)

        # 转发动态
        major = mod_post.get("major") or {}
        orig = major.get("orig")
        if orig:
            orig_desc = (orig.get("desc") or {}).get("text") or ""
            orig_author = ((orig.get("modules") or {}).get("module_author") or {}).get("name") or ""
            if not orig_desc:
                # polymer 转发的原动态可能在 modules 里
                orig_modules = orig.get("modules") or {}
                orig_mod = orig_modules.get("module_dynamic") or {}
                orig_desc = (orig_mod.get("desc") or {}).get("text") or ""
                orig_author = (orig_modules.get("module_author") or {}).get("name") or orig_author
                if not orig_desc:
                    # rich_text_nodes
                    orig_nodes = (orig_mod.get("desc") or {}).get("rich_text_nodes", [])
                    orig_parts = []
                    for node in orig_nodes:
                        if node.get("type") == "RICH_TEXT_NODE_TYPE_TEXT":
                            orig_parts.append(node.get("text", ""))
                        elif node.get("type") == "RICH_TEXT_NODE_TYPE_EMOJI":
                            emoji = node.get("emoji") or {}
                            orig_parts.append(emoji.get("text", "[表情]"))
                        else:
                            orig_parts.append(node.get("text", ""))
                    orig_desc = "".join(orig_parts)
            if orig_desc:
                text = f"{text}\n//@{orig_author}：{orig_desc}"

        # 扩展动态类型的特殊文本（OPUS、ARTICLE 等）
        major_type = major.get("type") or ""

        if major_type == "MAJOR_TYPE_OPUS":
            opus = major.get("opus") or {}
            # 从 opus.summary 提取文本（含 rich_text_nodes）
            opus_text = JiaranPostPlugin._extract_text_from_summary(opus.get("summary") or {})
            if opus_text:
                text = opus_text
            # 从 opus 提取图片
            for pic in opus.get("pics", []):
                src = pic.get("url", "")
                if src:
                    img_urls.append(src)
            # title
            title = opus.get("title") or ""
            if title and title not in text:
                text = f"{text}\n【{title}】" if text else f"【{title}】"

        elif major_type == "MAJOR_TYPE_DRAW":
            draw = major.get("draw") or {}
            for draw_item in draw.get("items", []):
                src = draw_item.get("src", "")
                if src:
                    img_urls.append(src)

        elif major_type == "MAJOR_TYPE_ARCHIVE":
            archive = major.get("archive") or {}
            cover = archive.get("cover", "")
            if cover:
                img_urls.append(cover)

        elif major_type == "MAJOR_TYPE_ARTICLE":
            article = major.get("article") or {}
            article_text = article.get("title") or article.get("desc") or ""
            if article_text:
                text = article_text if not text.strip() else f"{text}\n【{article_text}】"
            for pic in article.get("covers", []):
                if pic:
                    img_urls.append(pic)

        # 最终兜底文案
        if not text.strip():
            if img_urls:
                text = f"[分享了{len(img_urls)}张图片]"
            elif major_type == "MAJOR_TYPE_ARTICLE":
                text = "[分享了专栏文章]"
            elif major_type == "MAJOR_TYPE_ARCHIVE":
                text = f"[分享了视频：{(major.get('archive') or {}).get('title', '')}]"
            elif major_type == "MAJOR_TYPE_LIVE_RCMD":
                text = "[分享了直播间]"
            elif major_type == "MAJOR_TYPE_OPUS":
                text = "[分享了图文]"

        return text, pub_ts, post_id, img_urls

    # ==================== 图片下载 ====================

    async def _download_images(self, img_urls: list) -> list:
        """下载图片到本地，返回本地路径列表"""
        downloaded = []
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": "https://www.bilibili.com/",
        }
        async with httpx.AsyncClient() as client:
            for url in img_urls[:9]:
                try:
                    resp = await client.get(url, headers=headers, timeout=20)
                    if resp.status_code == 200:
                        ct = resp.headers.get("content-type", "")
                        ext = ".jpg"
                        if "png" in ct:
                            ext = ".png"
                        elif "gif" in ct:
                            ext = ".gif"
                        elif "webp" in ct:
                            ext = ".webp"
                        filepath = os.path.join(self._img_dir, f"{abs(hash(url))}{ext}")
                        with open(filepath, "wb") as f:
                            f.write(resp.content)
                        downloaded.append(filepath)
                except Exception as e:
                    logger.warning(f"[抽然动态] 图片下载失败 {url[:60]}: {e}")
        return downloaded

    # ==================== LLM 评论 ====================

    async def _get_llm_comment(self, post_text: str, time_str: str = "") -> str:
        """调用LLM以BOT人设评价动态"""
        if post_text.strip():
            content_desc = f"动态内容：\n{post_text.strip()}"
        else:
            content_desc = "这条动态是纯图片/视频动态，没有文字描述。"

        time_line = f"该动态的发布时间是 {time_str}。" if time_str else ""

        prompt = (
            f"下面是一条来自虚拟偶像「嘉然今天吃什么」在B站发布的动态。"
            f"{time_line}"
            f"请你以你的人物设定，从粉丝视角对这条动态发表一段简短的评论，"
            f"要求语气生动活泼、有真实粉丝的情感，不超过150字。"
            f"直接输出评论即可，不要加任何前缀。\n\n"
            f"{content_desc}"
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
