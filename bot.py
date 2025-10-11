import os
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Set, Tuple

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters
)
from telethon import TelegramClient, events, functions
from telethon.tl.functions.account import UpdateProfileRequest

# ======================= CONFIG =======================
BOT_TOKEN = "8388938837:AAFLBd4BHMUnbwelsqcXbsjtuz6t7-nTZoc"  # <-- Updated Bot Token
API_ID = 24945402
API_HASH = "6118e50f5dc4e3a955e50b22cf673ae2"

FORCE_CHANNEL = "@CosmicAdsPro"
FORCE_GROUP = "@Cosmicadsgroup"
PRIVACY_LINK = "https://gist.github.com/harshpvt1029-svg/504fba01171ef14c81f9f7143f5349c5#file-privacy-policy"

ADMIN_IDS: Set[int] = {7769531937, 7609459487, 8463150711}
WATERMARK = " - Via @CosmicAdsBot"
PREMIUM_PRICE_TEXT = "299₹ / month"

# ======================= LOGGING =======================
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    filename="bot.log",
    filemode="a"
)
logger = logging.getLogger(__name__)

# ======================= STORAGE =======================
user_sessions: Dict[int, str] = {}
user_premium_expiry: Dict[int, datetime] = {}
known_users: Set[int] = set()
user_ad_message: Dict[int, Dict[str, Optional[str]]] = {}
user_ad_interval: Dict[int, int] = {}
user_reply_interval: Dict[int, int] = {}
auto_reply_keywords: Dict[int, Dict[str, str]] = {}
last_reply_times: Dict[Tuple[int, int, str], datetime] = {}
ads_running: Set[int] = set()
telethon_clients: Dict[int, TelegramClient] = {}
user_logs: Dict[int, list] = {}
pending_add_message: Set[int] = set()
group_fetch_lock = asyncio.Lock()

# ======================= HELPERS =======================
def session_name(user_id: int) -> str:
    os.makedirs("sessions", exist_ok=True)
    return f"sessions/{user_id}"

def is_premium(user_id: int) -> bool:
    expiry = user_premium_expiry.get(user_id)
    return expiry is not None and expiry > datetime.now()

def build_main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("👤 Add Account", callback_data="add_account")],
        [InlineKeyboardButton("📊 Add Groups", callback_data="add_groups"),
         InlineKeyboardButton("📝 Add Message", callback_data="add_message")],
        [InlineKeyboardButton("⏲️ Set Ad Interval", callback_data="set_ad_intervals")],
        [InlineKeyboardButton("🎬 Start/Stop Ads", callback_data="toggle_ads"),
         InlineKeyboardButton("📜 Logs", callback_data="logs")],
        [InlineKeyboardButton("🔒 Logout", callback_data="logout")]
    ]
    if is_premium(user_id):
        rows.append([InlineKeyboardButton("💬 Auto Reply (Premium)", callback_data="auto_reply")])
    rows.append([InlineKeyboardButton("🌟 Premium", callback_data="premium")])
    return InlineKeyboardMarkup(rows)

def build_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="back_to_dashboard")]]
    )

async def user_is_member(bot, user_id: int, chat: str) -> bool:
    try:
        member = await bot.get_chat_member(chat, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

def _log(user_id: int, kind: str, chat_id: int, detail: str, status: str):
    arr = user_logs.setdefault(user_id, [])
    arr.append((datetime.now().strftime("%Y-%m-%d %H:%M:%S"), kind, chat_id, detail, status))
    if len(arr) > 300:
        del arr[:-300]
    logger.info(f"[LOG] user={user_id} kind={kind} chat={chat_id} detail='{detail}' status={status}")

# ======================= WATERMARK / BIO ENFORCER =======================
async def enforce_promo_profile(user_id: int, client: TelegramClient):
    try:
        me = await client.get_me()
        need_name = False
        new_first = (me.first_name or "")
        if not new_first.endswith(WATERMARK):
            new_first = (me.first_name or "") + WATERMARK
            need_name = True

        desired_bio = "Free Ads Bot @CosmicAdsBot"
        try:
            info = await client(functions.account.GetUserInfoRequest())
            current_bio = info.about or ""
        except Exception:
            current_bio = ""

        need_bio = (current_bio != desired_bio)
        if need_name or need_bio:
            await client(UpdateProfileRequest(
                first_name=new_first if need_name else None,
                about=desired_bio if need_bio else None
            ))
            logger.info(f"[EnforceProfile] Updated name/bio for user={user_id}")

    except Exception as e:
        logger.error(f"[EnforceProfile Error] user={user_id} err={e}")

async def profile_watchdog(user_id: int, client: TelegramClient):
    while telethon_clients.get(user_id) is client:
        try:
            if not is_premium(user_id):
                await enforce_promo_profile(user_id, client)
        except Exception as e:
            logger.error(f"[ProfileWatchdog] {e}")
        await asyncio.sleep(300)

# ======================= TELETHON CLIENT BOOT =======================
async def ensure_telethon(user_id: int) -> Optional[TelegramClient]:
    cli = telethon_clients.get(user_id)
    if cli:
        try:
            if await cli.is_user_authorized():
                return cli
        except Exception:
            pass
        try:
            await cli.disconnect()
        except Exception:
            pass
        telethon_clients.pop(user_id, None)

    sess = session_name(user_id)
    client = TelegramClient(sess, API_ID, API_HASH, connection_retries=10, timeout=10)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            return None
        telethon_clients[user_id] = client
        asyncio.create_task(profile_watchdog(user_id, client))
        return client
    except Exception as e:
        logger.error(f"[ensure_telethon] user={user_id} err={e}")
        return None

# ======================= AUTO-ADS LOOP =======================
async def auto_ads_loop(user_id: int):
    client = await ensure_telethon(user_id)
    if not client:
        return

    while user_id in ads_running:
        try:
            ad = user_ad_message.get(user_id)
            if not ad or not (ad.get("text") or ad.get("photo")):
                await asyncio.sleep(5)
                continue

            interval = max(1, user_ad_interval.get(user_id, 5))
            sent_any = False
            async for dialog in client.iter_dialogs():
                if dialog.is_user:
                    continue
                entity = dialog.entity
                is_megagroup = bool(getattr(entity, "megagroup", False))
                is_broadcast = bool(getattr(entity, "broadcast", False))
                if dialog.is_group or is_megagroup:
                    try:
                        if ad.get("photo"):
                            await client.send_file(dialog.id, ad["photo"], caption=ad.get("text") or "")
                        else:
                            await client.send_message(dialog.id, ad["text"] or "")
                        _log(user_id, "ads", dialog.id, "sent", "OK")
                        sent_any = True
                        await asyncio.sleep(0.4)
                    except Exception as e:
                        _log(user_id, "ads", dialog.id, f"failed:{e}", "FAIL")
                        await asyncio.sleep(0.4)
                elif is_broadcast and not is_megagroup:
                    continue
            await asyncio.sleep(interval * 60 if sent_any else 10)
        except Exception as e:
            logger.error(f"[auto_ads_loop] user={user_id} err={e}")
            await asyncio.sleep(5)

# ======================= AUTO-REPLY =======================
async def ensure_autoreply_handlers(user_id: int):
    if not is_premium(user_id):
        return
    client = await ensure_telethon(user_id)
    if not client:
        return

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        try:
            if event.is_private:
                return
            if event.is_channel:
                entity = await event.get_chat()
                if not getattr(entity, "megagroup", False):
                    return
            text = (event.raw_text or "").lower()
            if not text:
                return
            user_keywords = auto_reply_keywords.get(user_id, {})
            if not user_keywords:
                return
            interval = max(1, user_reply_interval.get(user_id, 5))
            now = datetime.now()
            for keyword, reply in user_keywords.items():
                if keyword in text:
                    key = (user_id, event.chat_id, keyword)
                    last_t = last_reply_times.get(key)
                    if (last_t is None) or (now - last_t > timedelta(minutes=interval)):
                        try:
                            await event.reply(reply)
                            _log(user_id, "reply", event.chat_id, keyword, "OK")
                        except Exception as e:
                            _log(user_id, "reply", event.chat_id, f"{keyword}:{e}", "FAIL")
                        last_reply_times[key] = now
                        break
        except Exception as e:
            logger.error(f"[AutoReply Handler] user={user_id} error={e}")
    asyncio.create_task(client.run_until_disconnected())

# ======================= /start =======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id  # This should be indented by 4 spaces
    known_users.add(user_id)
    if not await user_is_member(context.bot, user_id, FORCE_CHANNEL) or not await user_is_member(context.bot, user_id, FORCE_GROUP):
        keyboard = [[InlineKeyboardButton("✅ I have read and joined", callback_data="joined")]]
        spacer = "\u200b\u200b\u200b"
        text = (
            f"✨ *Welcome to Cosmic Ads Bot* ✨{spacer}\n\n"
            f"Please read our Privacy Policy:\n{PRIVACY_LINK}{spacer}\n\n"
            f"Before continuing, please join:\n{FORCE_CHANNEL}\n{FORCE_GROUP}"
        )
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return
    await update.message.reply_text("Welcome to your dashboard:", reply_markup=build_main_keyboard(user_id))

# ======================= BUTTON HANDLER =======================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    logger.info(f"[Button] user={user_id} data={data}")

    if data == "back_to_dashboard":
        await query.edit_message_text("Dashboard:", reply_markup=build_main_keyboard(user_id))
        return
    if data == "joined":
        if await user_is_member(context.bot, user_id, FORCE_CHANNEL) and await user_is_member(context.bot, user_id, FORCE_GROUP):
            await query.edit_message_text("Thank you for joining! Dashboard:", reply_markup=build_main_keyboard(user_id))
        else:
            await query.answer("You have not joined both required groups/channels!", show_alert=True)
        return
    if data == "add_account":
        if os.path.exists(session_name(user_id) + ".session"):
            user_sessions[user_id] = session_name(user_id)
            await query.edit_message_text("✅ You are already logged in!", reply_markup=build_main_keyboard(user_id))
        else:
            await query.edit_message_text("👤 Please log in first using the Login Bot: @CosmicLogin2bot", reply_markup=build_back_keyboard())
        return
    if data == "add_groups":
        if not os.path.exists(session_name(user_id) + ".session"):
            await query.edit_message_text("❌ You need to log in first! Use @CosmicLogin2bot", reply_markup=build_back_keyboard())
            return
        async with group_fetch_lock:
            client = await ensure_telethon(user_id)
            if not client:
                await query.edit_message_text("⚠️ Session not authorized. Please log in again.", reply_markup=build_back_keyboard())
                return
            try:
                groups = []
                async for dialog in client.iter_dialogs():
                    entity = dialog.entity
                    is_megagroup = bool(getattr(entity, "megagroup", False))
                    if dialog.is_group or is_megagroup:
                        groups.append(dialog.title)
                if groups:
                    group_list = "\n".join([f"• {g}" for g in groups[:50]])
                    await query.edit_message_text(f"📊 Here are your groups:\n\n{group_list}", reply_markup=build_back_keyboard())
                else:
                    await query.edit_message_text("No groups found.", reply_markup=build_back_keyboard())
            except Exception as e:
                await query.edit_message_text(f"⚠️ Failed to fetch groups: {e}", reply_markup=build_back_keyboard())
        return
    if data == "add_message":
        if not os.path.exists(session_name(user_id) + ".session"):
            await query.edit_message_text("❌ You need to log in first! Use @CosmicLogin2bot", reply_markup=build_back_keyboard())
            return
        pending_add_message.add(user_id)
        await query.edit_message_text(
            "📝 Send the *message you want to promote*.\n\n"
            "• You can send *text only*, or *photo with caption*.\n"
            "• Line breaks are preserved.\n\n"
            "_After you send, it will be saved as your active promo._",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_back_keyboard()
        )
        return
    if data == "set_ad_intervals":
        keyboard = [
            [InlineKeyboardButton("2 minutes", callback_data="adint_2")],
            [InlineKeyboardButton("5 minutes", callback_data="adint_5")],
            [InlineKeyboardButton("10 minutes", callback_data="adint_10")],
            [InlineKeyboardButton("15 minutes", callback_data="adint_15")],
            [InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="back_to_dashboard")],
        ]
        await query.edit_message_text("Choose *Ad* interval:", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("adint_"):
        try:
            minutes = int(data.split("_")[1])
        except Exception:
            minutes = 5
        user_ad_interval[user_id] = minutes
        await query.edit_message_text(f"⏲️ Ad interval set to *{minutes} min*.", parse_mode=ParseMode.MARKDOWN, reply_markup=build_main_keyboard(user_id))
        return

    if data == "toggle_ads":
        if user_id in ads_running:
            ads_running.remove(user_id)
            await query.edit_message_text("✅ Auto-ads stopped.", reply_markup=build_main_keyboard(user_id))
        else:
            ad = user_ad_message.get(user_id)
            if not ad or not (ad.get("text") or ad.get("photo")):
                await query.edit_message_text("❌ No message set. Tap *Add Message* first.", parse_mode=ParseMode.MARKDOWN, reply_markup=build_main_keyboard(user_id))
                return
            ads_running.add(user_id)
            asyncio.create_task(auto_ads_loop(user_id))
            await query.edit_message_text("🚀 Auto-ads started (groups only)", reply_markup=build_main_keyboard(user_id))
        return

    if data == "logs":
        logs = user_logs.get(user_id, [])
        if not logs:
            await query.edit_message_text("No logs yet.", reply_markup=build_back_keyboard())
        else:
            view = logs[-30:]
            lines = [f"{ts} | {kind} | chat:{cid} | {detail} | {st}" for (ts, kind, cid, detail, st) in view]
            msg = "📜 *Recent Logs* (latest 30)\n" + "\n".join(lines)
            if len(msg) > 3800:
                with open("logs.txt", "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
                await query.edit_message_text("Logs are long; sending as file…", reply_markup=build_back_keyboard())
                await context.bot.send_document(user_id, InputFile("logs.txt"))
                os.remove("logs.txt")
            else:
                await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_keyboard())
        return

    if data == "logout":
        path = session_name(user_id) + ".session"
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass
        user_sessions.pop(user_id, None)
        client = telethon_clients.pop(user_id, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        ads_running.discard(user_id)
        await query.edit_message_text("Logged out.", reply_markup=build_main_keyboard(user_id))
        return

# ======================= MESSAGE HANDLER =======================
async def capture_add_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in pending_add_message:
        return
    text = update.message.caption if update.message.photo else update.message.text
    text = text or ""
    photo_id = None
    if update.message.photo:
        photo_id = update.message.photo[-1].file_id
    user_ad_message[user_id] = {"text": text, "photo": photo_id}
    pending_add_message.discard(user_id)
    preview = "(with photo)" if photo_id else "(text only)"
    await update.message.reply_text(f"✅ Message saved {preview}.", reply_markup=build_main_keyboard(user_id))

# ======================= COMMANDS =======================
async def set_auto_reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_premium(user_id):
        await update.message.reply_text("❌ Auto-Reply is Premium only. Buy from *Premium* menu.", parse_mode=ParseMode.MARKDOWN)
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /set_auto_reply <keyword> <reply>")
        return
    keyword = args[0].lower()
    reply = " ".join(args[1:])
    auto_reply_keywords.setdefault(user_id, {})[keyword] = reply
    await update.message.reply_text(f"✅ Auto-reply set for '{keyword}'.")
    await ensure_autoreply_handlers(user_id)

async def off_keyword_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_premium(user_id):
        return
    text = update.message.text or ""
    if not text.startswith("/off_"):
        return
    keyword = text[5:].strip().lower()
    user_map = auto_reply_keywords.get(user_id, {})
    if keyword in user_map:
        del user_map[keyword]
        await update.message.reply_text(f"🛑 Auto-reply for '{keyword}' turned OFF.")
    else:
        await update.message.reply_text(f"No running auto-reply found for '{keyword}'.")

# ---- Admin: approve / unapprove premium ----
async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" ❌ Not authorized.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /approve <user_id>")
        return
    try:
        approved_user = int(context.args[0])
    except Exception:
        await update.message.reply_text("Invalid user ID.")
        return
    user_premium_expiry[approved_user] = datetime.now() + timedelta(days=30)
    await update.message.reply_text(f"✅ User {approved_user} Premium for 30 days.")
    try:
        await context.bot.send_message(approved_user, "🎉 Premium activated (30 days)! 🚀")
    except Exception:
        pass

async def unapprove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" ❌ Not authorized.")
        return
    if len(context.args) != 1:
        await update.message.reply_text("Usage: /unapprove <user_id>")
        return
    try:
        target_user = int(context.args[0])
    except Exception:
        await update.message.reply_text("Invalid user ID.")
        return
    if target_user in user_premium_expiry:
        user_premium_expiry.pop(target_user, None)
        await update.message.reply_text(f"✅ User {target_user} Premium removed.")
        try:
            await context.bot.send_message(target_user, "⚠️ Your Premium has been revoked by admin.")
        except Exception:
            pass
    else:
        await update.message.reply_text("⚠️ This user is not Premium.")

# ---- Admin: broadcast ----
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" ❌ Not authorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg = " ".join(context.args)
    targets = set(known_users)
    sent, failed = 0, 0
    for uid in targets:
        try:
            await context.bot.send_message(uid, f"📢 {msg}")
            sent += 1
            await asyncio.sleep(0.2)
        except Exception as e:
            failed += 1
            logger.error(f"[Broadcast] failed uid={uid} err={e}")
    await update.message.reply_text(f"✅ Broadcast complete.\nSent: {sent}\nFailed: {failed}")

# ======================= ADMIN NEW FEATURES =======================
async def checkusers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" ❌ Not authorized.")
        return
    count = 0
    for uid in known_users:
        try:
            user = await context.bot.get_chat(uid)
            name = (user.first_name or "") + (" " + user.last_name if getattr(user, "last_name", None) else "")
            if "@cosmicadsbot" in name.lower():
                count += 1
        except Exception:
            continue
    await update.message.reply_text(f"👥 Users with @CosmicAdsBot in name: {count}")

async def adstats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text(" ❌ Not authorized.")
        return
    now = datetime.now()
    today, week, month = 0, 0, 0
    for logs in user_logs.values():
        for ts, kind, cid, detail, st in logs:
            if kind == "ads" and st == "OK":
                t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                if t.date() == now.date():
                    today += 1
                if now - timedelta(days=7) <= t <= now:
                    week += 1
                if now - timedelta(days=30) <= t <= now:
                    month += 1
    await update.message.reply_text(
        f"📊 *Ad Stats*\n\n🗓️ Today: {today}\n📅 This Week: {week}\n📆 This Month: {month}",
        parse_mode=ParseMode.MARKDOWN,
    )

# ======================= MAIN =======================
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # Register handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set_auto_reply", set_auto_reply_cmd))
    application.add_handler(CommandHandler("approve", approve_cmd))
    application.add_handler(CommandHandler("unapprove", unapprove_cmd))
    application.add_handler(CommandHandler("broadcast", broadcast_cmd))
    application.add_handler(MessageHandler(filters.Regex(r"^/off_.+"), off_keyword_cmd))
    application.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, capture_add_message))
    application.add_handler(CallbackQueryHandler(button_handler))

    # ✅ New admin commands
    application.add_handler(CommandHandler("checkusers", checkusers_cmd))
    application.add_handler(CommandHandler("adstats", adstats_cmd))

    logger.info("Main Bot is running…")
    application.run_polling()

if __name__ == "__main__":
    main()
