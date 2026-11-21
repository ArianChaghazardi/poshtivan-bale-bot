#!/usr/bin/env python3
"""
Poshtivan Bale Master Bot Server
Centralized Two-Way Bot Service for WordPress Poshtivan Plugin
Supports Single Official Bale Bot with Dynamic Multi-Site Pairing & Two-Way Replies
Strict Markdown Formatting for Bale Messenger (Zero Raw HTML)

Requirements:
    pip install aiohttp

Usage:
    python3 bot.py [config.json]

Author: آرین چقازردی
Developer: هکتورتیم
"""

import os
import sys
import json
import sqlite3
import logging
import asyncio
import re
from typing import Dict, Any, Optional, List
from aiohttp import web, ClientSession, ClientTimeout

# Logging Configuration
logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger("PoshtivanBaleMaster")

CONFIG_FILE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "config.json")

DEFAULT_CONFIG = {
    "bot_token": "653449240:nw9TZy9N8TXJq5pbffxTb7gcVkMjj9rH4wc",
    "bot_username": "poshtivanbot",
    "mode": "polling",  # "polling" or "webhook"
    "webhook_url": "",
    "api_host": "127.0.0.1",
    "api_port": 8092,
    "bale_api_url": "https://tapi.bale.ai",
    "database_file": os.path.join(os.path.dirname(__file__), "bale_master.db"),
    "server_secret": ""
}

def load_config() -> Dict[str, Any]:
    cfg = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                user_cfg = json.load(f)
                cfg.update(user_cfg)
            logger.info(f"Loaded config from {CONFIG_FILE}")
        except Exception as e:
            logger.error(f"Error reading config file {CONFIG_FILE}: {e}")
    else:
        logger.warning(f"Config file not found at {CONFIG_FILE}, creating default template.")
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    return cfg

def html_to_bale_markdown(text: str) -> str:
    """
    Converts HTML markup into standard Bale-compatible Markdown.
    Bale does not support HTML tags and renders them as literal text.
    """
    if not text:
        return ""
    # Convert <br> and <p>
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>", "\n\n", text, flags=re.IGNORECASE)

    # Convert bold <b>, <strong> -> *text*
    text = re.sub(r"<(?:b|strong)>(.*?)</(?:b|strong)>", r"*\1*", text, flags=re.IGNORECASE | re.DOTALL)

    # Convert inline code <code> -> `code`
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.IGNORECASE | re.DOTALL)

    # Convert preformatted <pre> -> ```text```
    text = re.sub(r"<pre>(.*?)</pre>", r"```\n\1\n```", text, flags=re.IGNORECASE | re.DOTALL)

    # Convert italic <i>, <em> -> _text_
    text = re.sub(r"<(?:i|em)>(.*?)</(?:i|em)>", r"_\1_", text, flags=re.IGNORECASE | re.DOTALL)

    # Convert links <a href="url">text</a> -> [text](url)
    text = re.sub(r"<a\s+[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"[\2](\1)", text, flags=re.IGNORECASE | re.DOTALL)

    # Strip any remaining tags
    text = re.sub(r"<[^>]+>", "", text)

    # Unescape HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"").replace("&#039;", "'").replace("&nbsp;", " ")
    return text.strip()


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sites (
                    token TEXT PRIMARY KEY,
                    site_url TEXT NOT NULL,
                    site_name TEXT,
                    secret_key TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS connections (
                    token TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    connected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (token, chat_id)
                );
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS message_mappings (
                    bot_message_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    token TEXT NOT NULL,
                    site_url TEXT NOT NULL,
                    context_type TEXT NOT NULL,
                    context_id INTEGER NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (bot_message_id, chat_id)
                );
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_conn_token ON connections(token);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_conn_chat ON connections(chat_id);")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_msg_map ON message_mappings(bot_message_id, chat_id);")
            conn.commit()
            logger.info("Database tables verified.")

    def register_site(self, token: str, site_url: str, site_name: str = "", secret_key: str = ""):
        token = token.strip().upper()
        site_url = site_url.rstrip("/")
        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO sites (token, site_url, site_name, secret_key)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(token) DO UPDATE SET
                    site_url = excluded.site_url,
                    site_name = excluded.site_name,
                    secret_key = CASE WHEN excluded.secret_key != '' THEN excluded.secret_key ELSE sites.secret_key END;
            """, (token, site_url, site_name, secret_key))
            conn.commit()

    def get_site(self, token: str) -> Optional[Dict[str, Any]]:
        token = token.strip().upper()
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM sites WHERE token = ?", (token,)).fetchone()
            return dict(row) if row else None

    def add_connection(self, token: str, chat_id: int, username: str = "", first_name: str = ""):
        token = token.strip().upper()
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO connections (token, chat_id, username, first_name, connected_at)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP);
            """, (token, chat_id, username, first_name))
            conn.commit()

    def remove_connection(self, token: str, chat_id: Optional[int] = None):
        token = token.strip().upper()
        with self._get_conn() as conn:
            if chat_id is not None:
                conn.execute("DELETE FROM connections WHERE token = ? AND chat_id = ?", (token, chat_id))
            else:
                conn.execute("DELETE FROM connections WHERE token = ?", (token,))
            conn.commit()

    def get_site_connections(self, token: str):
        token = token.strip().upper()
        with self._get_conn() as conn:
            rows = conn.execute("SELECT * FROM connections WHERE token = ?", (token,)).fetchall()
            return [dict(r) for r in rows]

    def get_user_connections(self, chat_id: int):
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT c.*, s.site_name, s.site_url
                FROM connections c
                LEFT JOIN sites s ON c.token = s.token
                WHERE c.chat_id = ?
            """, (chat_id,)).fetchall()
            return [dict(r) for r in rows]

    def record_message_mapping(self, bot_msg_id: int, chat_id: int, token: str, site_url: str, context_type: str, context_id: int):
        with self._get_conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO message_mappings (bot_message_id, chat_id, token, site_url, context_type, context_id)
                VALUES (?, ?, ?, ?, ?, ?);
            """, (bot_msg_id, chat_id, token, site_url, context_type, context_id))
            conn.commit()

    def get_message_mapping(self, bot_msg_id: int, chat_id: int) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT m.*, s.secret_key
                FROM message_mappings m
                LEFT JOIN sites s ON m.token = s.token
                WHERE m.bot_message_id = ? AND m.chat_id = ?
            """, (bot_msg_id, chat_id)).fetchone()
            return dict(row) if row else None


class BaleMasterBot:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.db = Database(config["database_file"])
        self.token = config["bot_token"]
        self.api_url = config.get("bale_api_url", "https://tapi.bale.ai").rstrip("/")
        self.session: Optional[ClientSession] = None
        self.running = False

    async def init_session(self):
        if not self.session:
            timeout = ClientTimeout(total=45)
            headers = {"User-Agent": "PoshtivanBaleBot/2.2.3 (+https://hectortm.ir)"}
            self.session = ClientSession(timeout=timeout, headers=headers)

    async def close_session(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def call_api(self, method: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        await self.init_session()
        url = f"{self.api_url}/bot{self.token}/{method}"
        try:
            async with self.session.post(url, json=params or {}) as resp:
                data = await resp.json()
                return data
        except Exception as e:
            logger.error(f"Error calling Bale API method {method}: {e}")
            return {"ok": False, "error": str(e)}

    async def send_message(self, chat_id: int, text: str, reply_markup: Any = None, reply_to_message_id: int = None) -> Optional[int]:
        # Always enforce Markdown conversion for Bale Messenger
        md_text = html_to_bale_markdown(text)
        payload = {
            "chat_id": chat_id,
            "text": md_text,
            "parse_mode": "Markdown"
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id

        res = await self.call_api("sendMessage", payload)
        if res.get("ok"):
            return res.get("result", {}).get("message_id")
        else:
            # Fallback without parse_mode if Markdown parsing failed in Bale
            logger.warning(f"sendMessage with Markdown failed in Bale ({res}), retrying as plain text...")
            plain_text = re.sub(r"[*_`]", "", md_text)
            payload["text"] = plain_text
            payload.pop("parse_mode", None)
            res2 = await self.call_api("sendMessage", payload)
            if res2.get("ok"):
                return res2.get("result", {}).get("message_id")
            logger.error(f"Failed to send message to Bale chat {chat_id}: {res2}")
            return None

    # Handle incoming Bale Updates
    async def process_update(self, update: Dict[str, Any]):
        try:
            if "message" in update:
                await self._handle_message(update["message"])
            elif "callback_query" in update:
                await self._handle_callback_query(update["callback_query"])
        except Exception as e:
            logger.exception(f"Unhandled exception processing Bale update: {e}")

    async def _handle_message(self, msg: Dict[str, Any]):
        chat_id = msg.get("chat", {}).get("id")
        from_user = msg.get("from", {})
        username = from_user.get("username", "")
        first_name = from_user.get("first_name", "")
        text = msg.get("text", "").strip()
        msg_id = msg.get("message_id")

        if not chat_id:
            return

        # Case A: /start command (with optional deep-link token parameter)
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                token_candidate = parts[1].strip().upper()
                await self._process_connect_token(chat_id, token_candidate, username, first_name)
                return

            welcome_text = (
                "سلام! به ربات رسمی پشتیوان در پیام رسان بله خوش آمدید. 🌟\n\n"
                "از طریق این ربات می توانید تمامی پیام های چت آنلاین و تیکت های پشتیبانی وبسایت های خود را به صورت لحظه ای دریافت کنید و مستقیما با زدن گزینه پاسخ (Reply) به کاربران پاسخ دهید.\n\n"
                "🔑 *نحوه اتصال به وبسایت شما:*\n"
                "وارد پیشخوان وردپرس سایت خود شوید، به بخش *پشتیوان > تنظیمات > ربات ها > تب بله* بروید و کد اختصاصی وبسایت خود را (مثال: `PST-12345678`) کپی کرده و به همین ربات ارسال نمایید.\n\n"
                "دستورات موجود:\n"
                "▫️ `/connect کد_سایت` - اتصال سایت به اکانت شما\n"
                "▫️ `/status` - مشاهده وضعیت سایت های متصل\n"
                "▫️ `/help` - راهنمای کار با ربات"
            )
            await self.send_message(chat_id, welcome_text)
            return

        # Case B: /connect command or plain token
        if text.startswith("/connect"):
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                token_candidate = parts[1].strip().upper()
                await self._process_connect_token(chat_id, token_candidate, username, first_name)
            else:
                await self.send_message(chat_id, "لطفا کد اتصال را به صورت `/connect PST-XXXXXXXX` وارد کنید.")
            return

        # Case C: User sent a raw token PST-XXXXXXXX
        if text.upper().startswith("PST-") and len(text) >= 10:
            token_candidate = text.strip().upper()
            await self._process_connect_token(chat_id, token_candidate, username, first_name)
            return

        # Case D: /status command
        if text.startswith("/status"):
            connections = self.db.get_user_connections(chat_id)
            if not connections:
                await self.send_message(
                    chat_id,
                    "⚠️ شما هنوز هیچ وبسایتی را به این اکانت متصل نکرده اید.\n"
                    "برای اتصال، کد اختصاصی سایت خود را از تنظیمات افزونه پشتیوان ارسال کنید."
                )
                return

            msg_lines = ["📋 *وبسایت های متصل به اکانت شما در بله:*\n"]
            for idx, c in enumerate(connections, 1):
                s_name = c.get("site_name") or "وبسایت"
                s_url = c.get("site_url") or "---"
                t = c.get("token")
                msg_lines.append(
                    f"🌐 *{s_name}*\n"
                    f"🔗 آدرس: {s_url}\n"
                    f"🔑 کد اتصال: `{t}`\n"
                    f"❌ قطع اتصال: /disconnect_{t.replace('-', '_')}\n"
                )
            await self.send_message(chat_id, "\n".join(msg_lines))
            return

        # Case E: /disconnect command
        if text.startswith("/disconnect"):
            clean_cmd = text.replace("/disconnect_", "/disconnect ").replace("_", "-")
            parts = clean_cmd.split(maxsplit=1)
            if len(parts) > 1:
                token_to_disc = parts[1].strip().upper()
                site = self.db.get_site(token_to_disc)
                self.db.remove_connection(token_to_disc, chat_id)
                await self.send_message(chat_id, f"✅ اتصال اکانت شما با سایت `{token_to_disc}` با موفقیت قطع شد.")

                # Notify WordPress of disconnection
                if site:
                    asyncio.create_task(self._notify_wordpress_admin_disconnected(site, token_to_disc, chat_id))
            else:
                await self.send_message(chat_id, "لطفا کد سایتی که قصد قطع اتصال آن را دارید وارد کنید:\n`/disconnect PST-XXXXXXXX`")
            return

        # Case F: /help command
        if text.startswith("/help"):
            help_text = (
                "📖 *راهنمای ربات بله پشتیوان:*\n\n"
                "۱. برای دریافت پیام های چت آنلاین، کد افزونه پشتیوان را از پیشخوان وردپرس سایت کپی کرده و به ربات ارسال نمایید.\n"
                "۲. وقتی پیامی از مشتری ارسال می شود، اعلان آن همراه با نام و شماره کاربر در این ربات دریافت می گردد.\n"
                "۳. جهت ارسال پاسخ به مشتری، کافیست روی همان پیام در بله گزینه *پاسخ (Reply)* را بزنید و پیام خود را بنویسید؛ پاسخ شما آنی در چت آنلاین برای کاربر نمایش داده می شود.\n"
                "۴. دستور `/status` لیست کلیه وبسایت های متصل را به شما نشان می دهد."
            )
            await self.send_message(chat_id, help_text)
            return

        # Case G: Admin Two-Way Reply (Replying to an existing message in Bale)
        reply_to = msg.get("reply_to_message")
        if reply_to:
            orig_msg_id = reply_to.get("message_id")
            mapping = self.db.get_message_mapping(orig_msg_id, chat_id)
            if mapping:
                logger.info(f"Admin {chat_id} replied to message {orig_msg_id} -> Context: {mapping['context_type']} #{mapping['context_id']}")
                await self._forward_reply_to_wordpress(chat_id, msg, mapping)
                return
            else:
                logger.warning(f"No mapping found for reply to message {orig_msg_id} from chat {chat_id}")
                await self.send_message(
                    chat_id,
                    "⚠️ اطلاعات گفتگوی مربوط به این پیام در ربات یافت نشد یا منقضی شده است.",
                    reply_to_message_id=msg_id
                )
                return

        # Unrecognized input
        await self.send_message(
            chat_id,
            "پیام شما متوجه نشدم. برای اتصال وبسایت خود، کد اختصاصی سایت (مثل `PST-12345678`) را ارسال کنید یا دستور /help را بزنید."
        )

    async def _process_connect_token(self, chat_id: int, token: str, username: str, first_name: str):
        if not re.match(r"^PST-[A-Z0-9]{8}$", token):
            await self.send_message(chat_id, "❌ قالب کد وارد شده نامعتبر است. کد سایت باید به صورت `PST-XXXXXXXX` باشد.")
            return

        # Save connection in DB
        self.db.add_connection(token, chat_id, username, first_name)
        site = self.db.get_site(token)

        success_text = (
            "🎉 *تبریک! اتصال با موفقیت برقرار شد.*\n\n"
            f"اکانت بله شما با موفقیت به وبسایت شما با کد اختصاصی `{token}` متصل گردید.\n\n"
            "🔔 از این پس تمامی پیام های جدید چت آنلاین و تیکت های مشتریان بلافاصله در همین ربات برای شما ارسال می گردد.\n"
            "✍️ جهت پاسخگویی به هر مشتری، کافیست روی پیام مربوطه گزینه *Reply* را بزنید."
        )
        await self.send_message(chat_id, success_text)

        # Sync connection back to WordPress site REST API
        if site:
            await self._notify_wordpress_admin_connected(site, token, chat_id, username, first_name)

    async def _notify_wordpress_admin_connected(self, site: Dict[str, Any], token: str, chat_id: int, username: str, first_name: str):
        url = f"{site['site_url'].rstrip('/')}/wp-json/poshtivan/v1/bot/central-sync"
        payload = {
            "platform": "bale",
            "token": token,
            "secret": site.get("secret_key", ""),
            "chat_id": chat_id,
            "username": username,
            "first_name": first_name,
            "status": "connected"
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "PoshtivanBaleBot/2.2.3 (+https://hectortm.ir)"
        }
        await self.init_session()
        try:
            async with self.session.post(url, json=payload, headers=headers, timeout=ClientTimeout(total=8)) as resp:
                data = await resp.json()
                logger.info(f"Synced Bale connection with WordPress site {site['site_url']}: {data}")
        except Exception as e:
            logger.warning(f"Could not notify WordPress site {site['site_url']} of Bale connection: {e}")

    async def _notify_wordpress_admin_disconnected(self, site: Dict[str, Any], token: str, chat_id: int):
        url = f"{site['site_url'].rstrip('/')}/wp-json/poshtivan/v1/bot/central-sync"
        payload = {
            "platform": "bale",
            "token": token,
            "secret": site.get("secret_key", ""),
            "chat_id": chat_id,
            "status": "disconnected"
        }
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "PoshtivanBaleBot/2.2.3 (+https://hectortm.ir)"
        }
        await self.init_session()
        try:
            async with self.session.post(url, json=payload, headers=headers, timeout=ClientTimeout(total=8)) as resp:
                await resp.json()
        except Exception as e:
            logger.warning(f"Error disconnecting from site: {e}")

    async def _forward_reply_to_wordpress(self, chat_id: int, msg: Dict[str, Any], mapping: Dict[str, Any]):
        site_url = mapping.get("site_url", "").rstrip("/")
        secret_key = mapping.get("secret_key", "")
        url = f"{site_url}/wp-json/poshtivan/v1/bot/webhook/bale?secret={secret_key}"

        # Construct standard update structure for WordPress
        forward_update = {
            "update_id": msg.get("message_id", 0),
            "message": msg,
            "context_type": mapping.get("context_type"),
            "context_id": mapping.get("context_id"),
            "token": mapping.get("token")
        }

        headers = {
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": secret_key,
            "User-Agent": "PoshtivanBaleBot/2.2.3 (+https://hectortm.ir)"
        }

        await self.init_session()
        try:
            async with self.session.post(url, json=forward_update, headers=headers, timeout=ClientTimeout(total=12)) as resp:
                if resp.status == 200:
                    resp_data = await resp.json()
                    logger.info(f"WordPress reply accepted: {resp_data}")
                    await self.send_message(
                        chat_id,
                        "✅ پاسخ شما با موفقیت برای مشتری ارسال شد.",
                        reply_to_message_id=msg.get("message_id")
                    )
                else:
                    text_err = await resp.text()
                    logger.error(f"Error forwarding reply to site: {resp.status} - {text_err}")
                    await self.send_message(
                        chat_id,
                        f"❌ خطا در ارسال پاسخ به وبسایت ({resp.status})",
                        reply_to_message_id=msg.get("message_id")
                    )
        except Exception as e:
            logger.exception(f"Exception forwarding reply to WordPress: {e}")
            await self.send_message(
                chat_id,
                f"❌ ارتباط با سرور سایت برقرار نشد: {e}",
                reply_to_message_id=msg.get("message_id")
            )

    async def _handle_callback_query(self, cb: Dict[str, Any]):
        cb_id = cb.get("id")
        from_user = cb.get("from", {})
        chat_id = from_user.get("id")
        orig_msg = cb.get("message", {})
        orig_msg_id = orig_msg.get("message_id")

        mapping = self.db.get_message_mapping(orig_msg_id, chat_id)
        if not mapping:
            await self.call_api("answerCallbackQuery", {
                "callback_query_id": cb_id,
                "text": "اطلاعات این گفتگو در پایگاه داده یافت نشد.",
                "show_alert": True
            })
            return

        site_url = mapping.get("site_url", "").rstrip("/")
        secret_key = mapping.get("secret_key", "")
        url = f"{site_url}/wp-json/poshtivan/v1/bot/webhook/bale?secret={secret_key}"

        forward_update = {
            "update_id": int(cb_id) if str(cb_id).isdigit() else 1,
            "callback_query": cb,
            "context_type": mapping.get("context_type"),
            "context_id": mapping.get("context_id"),
            "token": mapping.get("token")
        }

        headers = {
            "Content-Type": "application/json",
            "X-Telegram-Bot-Api-Secret-Token": secret_key,
            "User-Agent": "PoshtivanBaleBot/2.2.3 (+https://hectortm.ir)"
        }

        await self.init_session()
        try:
            async with self.session.post(url, json=forward_update, headers=headers, timeout=ClientTimeout(total=8)) as resp:
                data = await resp.json()
                alert_text = data.get("message", "درخواست با موفقیت ثبت شد.")
                await self.call_api("answerCallbackQuery", {
                    "callback_query_id": cb_id,
                    "text": alert_text,
                    "show_alert": False
                })
        except Exception as e:
            logger.error(f"Error forwarding callback query: {e}")
            await self.call_api("answerCallbackQuery", {
                "callback_query_id": cb_id,
                "text": "خطا در ارتباط با سرور سایت.",
                "show_alert": True
            })

    # Long Polling Loop
    async def run_polling(self):
        logger.info("Starting Bale long polling loop...")
        offset = 0
        self.running = True

        # Drop webhook if active before polling
        await self.call_api("deleteWebhook", {"drop_pending_updates": False})

        while self.running:
            try:
                updates_res = await self.call_api("getUpdates", {
                    "offset": offset,
                    "timeout": 30,
                    "allowed_updates": ["message", "callback_query"]
                })

                if updates_res.get("ok"):
                    updates = updates_res.get("result", [])
                    for update in updates:
                        up_id = update.get("update_id", 0)
                        if up_id >= offset:
                            offset = up_id + 1
                        asyncio.create_task(self.process_update(update))
                else:
                    err = updates_res.get("error", "Unknown error")
                    logger.warning(f"Bale getUpdates returned error: {err}. Retrying in 4s...")
                    await asyncio.sleep(4)
            except Exception as e:
                logger.error(f"Exception in Bale polling loop: {e}. Retrying in 5s...")
                await asyncio.sleep(5)


# Central API Web Server (FastAPI/aiohttp HTTP API)
def create_api_app(bot: BaleMasterBot) -> web.Application:
    app = web.Application()

    # Route 1: Register or Update Site Information
    async def api_register_site(request: web.Request) -> web.Response:
        try:
            data = await request.json()
            token = data.get("token", "").strip().upper()
            site_url = data.get("site_url", "").strip()
            site_name = data.get("site_name", "").strip()
            secret_key = data.get("secret_key", "").strip()

            if not token or not site_url:
                return web.json_response({"ok": False, "message": "Missing token or site_url"}, status=400)

            bot.db.register_site(token, site_url, site_name, secret_key)
            logger.info(f"Registered site: {token} -> {site_url} ({site_name})")

            # Check if any admins already connected
            connections = bot.db.get_site_connections(token)
            site = bot.db.get_site(token)
            if connections and site:
                for c in connections:
                    asyncio.create_task(bot._notify_wordpress_admin_connected(
                        site, token, c["chat_id"], c.get("username", ""), c.get("first_name", "")
                    ))

            return web.json_response({
                "ok": True,
                "message": "Site registered successfully",
                "connected_admins": len(connections)
            })
        except Exception as e:
            logger.exception(f"Error in api_register_site: {e}")
            return web.json_response({"ok": False, "message": str(e)}, status=500)

    # Route 2: Forward Notification to Connected Bale Admins
    async def api_notify(request: web.Request) -> web.Response:
        try:
            data = await request.json()
            token = data.get("token", "").strip().upper()
            secret = data.get("secret", "").strip()
            text = data.get("text", "")
            keyboard = data.get("keyboard")
            context_type = data.get("context_type", "chat")
            context_id = int(data.get("context_id", 0))
            site_url = data.get("site_url", "")
            site_name = data.get("site_name", "")

            if not token or not text:
                return web.json_response({"ok": False, "message": "Missing token or text"}, status=400)

            site = bot.db.get_site(token)
            if not site and site_url:
                bot.db.register_site(token, site_url, site_name, secret)
                site = bot.db.get_site(token)

            connections = bot.db.get_site_connections(token)
            if not connections:
                return web.json_response({
                    "ok": True,
                    "connected": False,
                    "sent_count": 0,
                    "message": "هیچ مدیری هنوز این کد سایت را در ربات بله متصل نکرده است."
                })

            reply_markup = None
            if keyboard and isinstance(keyboard, dict):
                reply_markup = keyboard
            elif keyboard and isinstance(keyboard, list):
                reply_markup = {"inline_keyboard": keyboard}

            sent_count = 0
            message_ids = []
            for c in connections:
                chat_id = c["chat_id"]
                bot_msg_id = await bot.send_message(chat_id, text, reply_markup=reply_markup)
                if bot_msg_id:
                    sent_count += 1
                    message_ids.append({"chat_id": chat_id, "message_id": bot_msg_id})
                    bot.db.record_message_mapping(
                        bot_msg_id,
                        chat_id,
                        token,
                        site_url or c.get("site_url", ""),
                        context_type,
                        context_id
                    )

            return web.json_response({
                "ok": True,
                "connected": True,
                "sent_count": sent_count,
                "message_ids": message_ids,
                "message": f"اعلان برای {sent_count} مدیر در بله ارسال شد."
            })
        except Exception as e:
            logger.exception(f"Error in api_notify: {e}")
            return web.json_response({"ok": False, "message": str(e)}, status=500)

    # Route 3: Check Site Status
    async def api_status(request: web.Request) -> web.Response:
        token = request.query.get("token", "").strip().upper()
        if not token:
            return web.json_response({"ok": False, "message": "Missing token"}, status=400)

        site_url = request.query.get("site_url", "").strip()
        site_name = request.query.get("site_name", "").strip()
        secret = request.query.get("secret", "").strip()
        if site_url:
            bot.db.register_site(token, site_url, site_name, secret)

        connections = bot.db.get_site_connections(token)
        site = bot.db.get_site(token)

        # Auto-sync any admins who already connected to this token
        if site and connections:
            for c in connections:
                asyncio.create_task(bot._notify_wordpress_admin_connected(
                    site, token, c["chat_id"], c.get("username", ""), c.get("first_name", "")
                ))

        admins = []
        for c in connections:
            admins.append({
                "chat_id": c["chat_id"],
                "username": c.get("username") or "",
                "first_name": c.get("first_name") or "",
                "connected_at": c.get("connected_at") or ""
            })

        return web.json_response({
            "ok": True,
            "token": token,
            "connected": len(admins) > 0,
            "admins_count": len(admins),
            "admins": admins,
            "bot_username": bot.config.get("bot_username", "poshtivanbot"),
            "site": site
        })

    # Route 4: Webhook Handler (if webhook mode enabled)
    async def api_webhook(request: web.Request) -> web.Response:
        try:
            update = await request.json()
            asyncio.create_task(bot.process_update(update))
            return web.json_response({"ok": True})
        except Exception as e:
            logger.error(f"Error in webhook endpoint: {e}")
            return web.json_response({"ok": False}, status=400)

    # Route 5: Disconnect
    async def api_disconnect(request: web.Request) -> web.Response:
        try:
            data = await request.json()
            token = data.get("token", "").strip().upper()
            chat_id = data.get("chat_id")
            if not token:
                return web.json_response({"ok": False, "message": "Missing token"}, status=400)
            bot.db.remove_connection(token, int(chat_id) if chat_id else None)
            return web.json_response({"ok": True, "message": "Connection removed"})
        except Exception as e:
            return web.json_response({"ok": False, "message": str(e)}, status=500)

    app.router.add_post("/api/register-site", api_register_site)
    app.router.add_post("/api/notify", api_notify)
    app.router.add_get("/api/status", api_status)
    app.router.add_post("/api/disconnect", api_disconnect)
    app.router.add_post("/webhook", api_webhook)

    return app

async def main():
    config = load_config()
    if not config.get("bot_token"):
        logger.error("bot_token is empty in config.json! Please provide a valid Bale Bot Token from @BotFather in Bale.")

    bot = BaleMasterBot(config)
    await bot.init_session()

    app = create_api_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()

    host = config.get("api_host", "127.0.0.1")
    port = int(config.get("api_port", 8092))
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info(f"Bale Master Bot API Server running at http://{host}:{port}")

    mode = config.get("mode", "polling")
    if mode == "polling":
        await bot.run_polling()
    elif mode == "webhook":
        wh_url = config.get("webhook_url", "")
        if wh_url:
            await bot.call_api("setWebhook", {"url": wh_url})
            logger.info(f"Set Bale webhook to {wh_url}")
        while True:
            await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Bale bot server stopped.")
