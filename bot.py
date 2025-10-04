import os
import asyncio
import logging
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    MessageHandler, filters
)
from telethon import TelegramClient, events, functions
from telethon.tl.functions.account import UpdateProfileRequest

# ---------------- CONFIG (your real values) ----------------
BOT_TOKEN = "8388938837:AAFLBd4BHMUnbwelsqcXbsjtuz6t7-nTZoc"
API_ID = 24945402
API_HASH = "6118e50f5dc4e3a955e50b22cf673ae2"

FORCE_CHANNEL = "@CosmicAdsPro"
FORCE_GROUP = "@Cosmicadsgroup"
PRIVACY_LINK = "https://gist.github.com/harshpvt1029-svg/504fba01171ef14c81f9f7143f5349c5#file-privacy-policy"

# Admin Telegram user IDs
ADMIN_IDS = {7769531937, 7609459487, 8463150711}

# Watermark for non-premium accounts
WATERMARK = " - Via @CosmicAdsBot"

# ---------------- LOGGING ----------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    filename="bot.log",
    filemode="a"
)
logger = logging.getLogger(__name__)

# ---------------- STORAGE (in-memory) ----------------
user_sessions = {}              # {user_id: "sessions/<id>"}
user_premium_expiry = {}        # {user_id: datetime}
user_referrals = {}             # {referrer_id: set(user_ids)}
user_balance_hours = {}         # {user_id: int} referral/admin hours (reset daily by original logic)
user_used_hours_today = {}      # {user_id: int}
last_reset = datetime.now().date()

auto_reply_keywords = {}        # {user_id: {keyword: reply}}
auto_reply_intervals = {}       # {user_id: minutes}
last_reply_times = {}           # {(user_id, chat_id, keyword): datetime}
ads_running = set()             # set(user_id) -> auto-reply loop enabled
telethon_clients = {}           # {user_id: TelegramClient}

admin_granted_any = set()       # users who received any admin hours (bypass 10h minimum to start)
user_logs = {}                  # {user_id: [(ts, chat_id, keyword, status), ...]}

group_fetch_lock = asyncio.Lock()  # avoids "database is locked" in add_groups

# ---------------- HELPERS ----------------
def session_name(user_id: int) -> str:
    os.makedirs("sessions", exist_ok=True)
    return f"sessions/{user_id}"

def build_main_keyboard() -> InlineKeyboardMarkup:
    # 1-2-1-2-1-2-1 pattern (decorated dashboard)
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👤 Add Account", callback_data="add_account")],  # 1
            [InlineKeyboardButton("📊 Add Groups", callback_data="add_groups"),
             InlineKeyboardButton("💬 Auto Reply", callback_data="auto_reply")],   # 2
            [InlineKeyboardButton("⏲️ Set Time Intervals", callback_data="set_intervals")],  # 1
            [InlineKeyboardButton("🎬 Start/Stop Ads", callback_data="toggle_ads"),
             InlineKeyboardButton("📜 Logs", callback_data="logs")],               # 2
            [InlineKeyboardButton("🔒 Logout", callback_data="logout")],           # 1
            [InlineKeyboardButton("🔗 Refer & Earn", callback_data="refer_earn"),
             InlineKeyboardButton("💰 My Balance", callback_data="my_balance")],   # 2
            [InlineKeyboardButton("🌟 Premium", callback_data="premium")],         # 1
        ]
    )

def build_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="back_to_dashboard")]]
    )

def is_premium(user_id: int) -> bool:
    expiry = user_premium_expiry.get(user_id)
    return expiry is not None and expiry > datetime.now()

def get_referral_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start={user_id}"

async def user_is_member(bot, user_id: int, chat: str) -> bool:
    try:
        member = await bot.get_chat_member(chat, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception:
        return False

def reset_daily_usage():
    """Original behavior: reset referral/admin hours + used counters daily."""
    global last_reset
    today = datetime.now().date()
    if today != last_reset:
        last_reset = today
        user_balance_hours.clear()
        user_used_hours_today.clear()
        logger.info("[Daily Reset] Cleared balances and daily usage.")

def _log(user_id: int, chat_id: int, keyword: str, status: str):
    arr = user_logs.setdefault(user_id, [])
    arr.append((datetime.now().strftime("%Y-%m-%d %H:%M:%S"), chat_id, keyword, status))
    if len(arr) > 200:
        del arr[:-200]  # keep last 200
    # also log to file
    logger.info(f"[LOG] user={user_id} chat={chat_id} key='{keyword}' status={status}")

# ---------------- WATERMARK ENFORCER ----------------
async def enforce_watermark(user_id: int, client: TelegramClient):
    if is_premium(user_id):
        return
    me = await client.get_me()
    changed = False
    try:
        if not me.first_name.endswith(WATERMARK):
            await client(UpdateProfileRequest(first_name=(me.first_name or "") + WATERMARK))
            changed = True
        info = await client(functions.account.GetUserInfoRequest())
        bio = info.about or ""
        if WATERMARK not in bio:
            await client(UpdateProfileRequest(about=(bio + WATERMARK) if bio else WATERMARK))
            changed = True
    except Exception as e:
        logger.error(f"[Watermark] Error for user {user_id}: {e}")
    if changed:
        logger.info(f"[Watermark] Updated for user {user_id}")

async def watermark_loop(user_id: int, client: TelegramClient):
    while user_id in telethon_clients:
        try:
            await enforce_watermark(user_id, client)
        except Exception as e:
            logger.error(f"[Watermark Loop Error] {e}")
        await asyncio.sleep(300)

# ---------------- TELETHON AUTO REPLY ----------------
async def start_auto_reply(user_id: int):
    """Start Telethon client + auto-reply handler for user_id. Cleans stale client first."""
    old = telethon_clients.get(user_id)
    if old:
        try:
            # If already connected, don't recreate.
            if hasattr(old, "is_connected") and old.is_connected():
                logger.info(f"[Telethon] Already running for {user_id}")
                return
            else:
                telethon_clients.pop(user_id, None)
        except Exception:
            telethon_clients.pop(user_id, None)

    session = session_name(user_id)
    client = TelegramClient(session, API_ID, API_HASH, connection_retries=10, timeout=10)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        logger.warning(f"[Telethon] Session not authorized for {user_id}")
        return

    # watermark loop (premium skips)
    try:
        await enforce_watermark(user_id, client)
        asyncio.create_task(watermark_loop(user_id, client))
    except Exception as e:
        logger.error(f"[Telethon] Watermark setup error for {user_id}: {e}")

    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        try:
            if user_id not in ads_running:
                return
            if not event.is_group:
                return

            reset_daily_usage()
            used = user_used_hours_today.get(user_id, 0)
            if not is_premium(user_id) and used >= 10:
                await stop_auto_reply(user_id)
                ads_running.discard(user_id)
                return

            text = event.raw_text.lower() if event.raw_text else ""
            if not text:
                return

            user_keywords = auto_reply_keywords.get(user_id, {})
            interval = auto_reply_intervals.get(user_id, 5)
            now = datetime.now()

            for keyword, reply in user_keywords.items():
                if keyword in text:
                    key = (user_id, event.chat_id, keyword)
                    last_time = last_reply_times.get(key)
                    if last_time is None or (now - last_time) > timedelta(minutes=interval):
                        try:
                            await event.reply(reply)
                            _log(user_id, event.chat_id, keyword, "Sent")
                        except Exception:
                            _log(user_id, event.chat_id, keyword, "Failed")
                        last_reply_times[key] = now
                        user_used_hours_today[user_id] = used + 1
                        break
        except Exception as e:
            logger.error(f"[AutoReply Handler] user={user_id} error={e}")

    asyncio.create_task(client.run_until_disconnected())
    telethon_clients[user_id] = client
    logger.info(f"[Telethon] Auto-reply started for {user_id}")

async def stop_auto_reply(user_id: int):
    client = telethon_clients.pop(user_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception as e:
            logger.error(f"[Telethon] Disconnect error for {user_id}: {e}")
        logger.info(f"[Telethon] Auto-reply stopped for {user_id}")

# ---------------- /start ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    reset_daily_usage()
    user_id = update.effective_user.id
    args = context.args
    logger.info(f"/start by {user_id} args={args}")

    # Referrals
    if args:
        try:
            referrer_id = int(args[0])
            if referrer_id != user_id:
                referred_set = user_referrals.setdefault(referrer_id, set())
                if user_id not in referred_set:
                    referred_set.add(user_id)
                    user_balance_hours[referrer_id] = user_balance_hours.get(referrer_id, 0) + 2
                    try:
                        await context.bot.send_message(
                            referrer_id,
                            f"🎉 You received 2 free hours for referring user {user_id}!"
                        )
                    except Exception:
                        pass
        except Exception:
            pass

    # Force-join gate
    if not await user_is_member(context.bot, user_id, FORCE_CHANNEL) or not await user_is_member(context.bot, user_id, FORCE_GROUP):
        keyboard = [[InlineKeyboardButton("✅ I have read and joined", callback_data="joined")]]
        text = (
            f"""✨ *Welcome to Cosmic Ads Bot* ✨

Please read our Privacy Policy:
{PRIVACY_LINK}

Before continuing, please join:
{FORCE_CHANNEL}
{FORCE_GROUP}"""
        )
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)
        return

    await update.message.reply_text("Welcome to your dashboard:", reply_markup=build_main_keyboard())

# ---------------- BUTTON HANDLER ----------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data
    logger.info(f"[Button] user={user_id} data={data}")
    reset_daily_usage()

    if data == "back_to_dashboard":
        await query.edit_message_text("Dashboard:", reply_markup=build_main_keyboard())

    elif data == "joined":
        if await user_is_member(context.bot, user_id, FORCE_CHANNEL) and await user_is_member(context.bot, user_id, FORCE_GROUP):
            await query.edit_message_text("Thank you for joining! Dashboard:", reply_markup=build_main_keyboard())
        else:
            await query.answer("You have not joined both required groups/channels!", show_alert=True)

    elif data == "add_account":
        if os.path.exists(session_name(user_id) + ".session"):
            user_sessions[user_id] = session_name(user_id)
            await query.edit_message_text("✅ You are already logged in!", reply_markup=build_main_keyboard())
        else:
            await query.edit_message_text("👤 Please log in first using the Login Bot: @CosmicLogin2Bot", reply_markup=build_back_keyboard())

    elif data == "add_groups":
        if not os.path.exists(session_name(user_id) + ".session"):
            await query.edit_message_text("❌ You need to log in first! Use @CosmicLogin2Bot", reply_markup=build_back_keyboard())
            return
        async with group_fetch_lock:  # prevent sqlite lock
            session = session_name(user_id)
            client = TelegramClient(session, API_ID, API_HASH, connection_retries=10, timeout=10)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await query.edit_message_text("⚠️ Session not authorized. Please log in again.", reply_markup=build_back_keyboard())
                    await client.disconnect()
                    return
                groups = []
                async for dialog in client.iter_dialogs():
                    if dialog.is_group or dialog.is_channel:
                        groups.append(dialog.title)
                await client.disconnect()
            except Exception as e:
                await query.edit_message_text(f"⚠️ Failed to fetch groups: {e}", reply_markup=build_back_keyboard())
                return

            if groups:
                group_list = "\n".join([f"• {g}" for g in groups[:50]])
                await query.edit_message_text(f"📊 Here are your groups:\n\n{group_list}", reply_markup=build_back_keyboard())
            else:
                await query.edit_message_text("No groups found.", reply_markup=build_back_keyboard())

    elif data == "auto_reply":
        # Show current keywords and off commands
        kws = list(auto_reply_keywords.get(user_id, {}).keys())
        if kws:
            lines = [f"• {k}  —  /off_{k}" for k in kws]
            msg = "🟢 *Currently running auto-replies:*\n" + "\n".join(lines)
        else:
            msg = "No auto-reply is currently running."

        if is_premium(user_id):
            msg += "\n\nYou can set multiple:\n`/set_auto_reply <keyword> <reply>`"
        else:
            msg += "\n\nFree tier runs only *one* at a time.\nSet new:\n`/set_auto_reply <keyword> <reply>`\n(Old one will be replaced automatically.)"
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_keyboard())

    elif data == "set_intervals":
        keyboard = [
            [InlineKeyboardButton("2 minutes", callback_data="interval_2")],
            [InlineKeyboardButton("5 minutes", callback_data="interval_5")],
            [InlineKeyboardButton("10 minutes", callback_data="interval_10")],
            [InlineKeyboardButton("15 minutes", callback_data="interval_15")],
            [InlineKeyboardButton("⬅️ Back to Dashboard", callback_data="back_to_dashboard")],
        ]
        await query.edit_message_text("Choose interval:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("interval_"):
        try:
            minutes = int(data.split("_")[1])
        except Exception:
            minutes = 5
        auto_reply_intervals[user_id] = minutes
        await query.edit_message_text(f"⏲️ Auto-reply interval set to {minutes} min.", reply_markup=build_main_keyboard())

    elif data == "toggle_ads":
        if user_id in ads_running:
            ads_running.remove(user_id)
            await stop_auto_reply(user_id)
            await query.edit_message_text("✅ Auto-reply stopped.", reply_markup=build_main_keyboard())
        else:
            balance = user_balance_hours.get(user_id, 0)
            used = user_used_hours_today.get(user_id, 0)
            if not is_premium(user_id):
                # Allow start if >=10h OR user has any admin-granted hours (even if <10)
                if balance < 10 and user_id not in admin_granted_any:
                    await query.edit_message_text("❌ Not enough referrals (need 10h).", reply_markup=build_main_keyboard())
                    return
                if used >= 10:
                    await query.edit_message_text("⏳ Daily limit reached (10h/24h).", reply_markup=build_main_keyboard())
                    return
            ads_running.add(user_id)
            await start_auto_reply(user_id)
            await query.edit_message_text("🚀 Auto-reply started!", reply_markup=build_main_keyboard())

    elif data == "logs":
        logs = user_logs.get(user_id, [])
        if not logs:
            await query.edit_message_text("No logs yet.", reply_markup=build_back_keyboard())
        else:
            view = logs[-20:]
            lines = [f"{ts} | chat:{cid} | '{kw}' | {st}" for (ts, cid, kw, st) in view]
            await query.edit_message_text("📜 *Recent Logs* (latest 20)\n" + "\n".join(lines),
                                          parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=build_back_keyboard())

    elif data == "logout":
        if os.path.exists(session_name(user_id) + ".session"):
            try:
                os.remove(session_name(user_id) + ".session")
            except OSError:
                pass
        user_sessions.pop(user_id, None)
        await stop_auto_reply(user_id)
        await query.edit_message_text("Logged out.", reply_markup=build_main_keyboard())

    elif data == "refer_earn":
        bot_username = (await context.bot.get_me()).username
        referral_link = get_referral_link(bot_username, user_id)
        count = len(user_referrals.get(user_id, set()))
        balance = user_balance_hours.get(user_id, 0)
        await query.edit_message_text(
            f"🔗 Referral:\n{referral_link}\n👥 Referred: {count}\n💰 Balance: {balance}h",
            reply_markup=build_back_keyboard()
        )

    elif data == "my_balance":
        balance = user_balance_hours.get(user_id, 0)
        used = user_used_hours_today.get(user_id, 0)
        premium_status = "✅ Yes" if is_premium(user_id) else "❌ No"
        await query.edit_message_text(
            f"💰 Balance: {balance}h\n⏳ Used today: {used}/10h\n🌟 Premium: {premium_status}",
            reply_markup=build_back_keyboard()
        )

    elif data == "premium":
        if is_premium(user_id):
            await query.edit_message_text("🌟 Premium is active.", reply_markup=build_back_keyboard())
        else:
            await query.edit_message_text(
                "🌟 *Premium Benefits:*\n\n✔ No watermark\n✔ Unlimited ads (1 month)\n\n💵 Cost: 399₹ / month\nTo buy: @LordHarsH",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_back_keyboard()
            )

# ---------------- COMMANDS ----------------
async def set_auto_reply_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /set_auto_reply <keyword> <reply>")
        return
    keyword = args[0].lower()
    reply = " ".join(args[1:])
    # Free tier: only one keyword at a time; premium: multiple
    if not is_premium(user_id):
        auto_reply_keywords[user_id] = {keyword: reply}
    else:
        auto_reply_keywords.setdefault(user_id, {})[keyword] = reply
    await update.message.reply_text(f"✅ Auto-reply set for '{keyword}' → {reply}")
    logger.info(f"[SetAutoReply] user={user_id} key='{keyword}'")

async def off_keyword_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    if not text.startswith("/off_"):
        return
    keyword = text[5:].strip().lower()
    uid = update.effective_user.id
    user_map = auto_reply_keywords.get(uid, {})
    if keyword in user_map:
        del user_map[keyword]
        await update.message.reply_text(f"🛑 Auto-reply for '{keyword}' turned OFF.")
    else:
        await update.message.reply_text(f"No running auto-reply found for '{keyword}'.")
    logger.info(f"[OffKeyword] user={uid} key='{keyword}'")

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
    logger.info(f"[Approve] admin={update.effective_user.id} user={approved_user}")

async def unapprove_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Not authorized.")
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
    logger.info(f"[Unapprove] admin={update.effective_user.id} user={target_user}")

# ---- Admin: add hours ----
async def add_hours_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Not authorized.")
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /add <hours> <user_id>")
        return
    try:
        hours = int(context.args[0])
        target = int(context.args[1])
    except Exception:
        await update.message.reply_text("Invalid arguments. Usage: /add <hours> <user_id>")
        return
    if hours <= 0:
        await update.message.reply_text("Hours must be positive.")
        return
    user_balance_hours[target] = user_balance_hours.get(target, 0) + hours
    admin_granted_any.add(target)  # bypass 10h minimum to start
    await update.message.reply_text(f"✅ Added {hours}h to user {target}. Balance now: {user_balance_hours[target]}h")
    try:
        await context.bot.send_message(target, f"🎁 Admin added {hours} free hours to your balance!")
    except Exception:
        pass
    logger.info(f"[AddHours] admin={update.effective_user.id} user={target} hours={hours}")

# ---- Admin: list all users ----
async def all_users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Not authorized.")
        return

    all_ids = set(list(user_sessions.keys()) +
                  list(user_premium_expiry.keys()) +
                  list(user_referrals.keys()) +
                  list(user_balance_hours.keys()))

    if not all_ids:
        await update.message.reply_text("No users found.")
        return

    lines = []
    for uid in all_ids:
        try:
            user_obj = await context.bot.get_chat(uid)
            name = user_obj.first_name or "Unknown"
            if getattr(user_obj, "last_name", None):
                name = f"{name} {user_obj.last_name}"
            lines.append(f"{name} ({uid})")
        except Exception:
            lines.append(f"Unknown ({uid})")

    text = "👥 *All Users:*\n" + "\n".join(lines)
    if len(text) > 4000:
        with open("all_users.txt", "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        await update.message.reply_document(InputFile("all_users.txt"))
        os.remove("all_users.txt")
    else:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    logger.info(f"[AllUsers] admin={update.effective_user.id} total={len(all_ids)}")

# ---- Admin: broadcast ----
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ Not authorized.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg = " ".join(context.args)

    all_ids = set(list(user_sessions.keys()) +
                  list(user_premium_expiry.keys()) +
                  list(user_referrals.keys()) +
                  list(user_balance_hours.keys()))
    sent = 0
    failed = 0
    for uid in all_ids:
        try:
            await context.bot.send_message(uid, f"📢 {msg}")
            sent += 1
            await asyncio.sleep(0.3)  # rate-limit a bit
        except Exception as e:
            failed += 1
            logger.error(f"[Broadcast] failed uid={uid} err={e}")
    await update.message.reply_text(f"✅ Broadcast complete.\nSent: {sent}\nFailed: {failed}")
    logger.info(f"[Broadcast] admin={update.effective_user.id} sent={sent} failed={failed}")

# ---------------- MAIN ----------------
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("set_auto_reply", set_auto_reply_cmd))
    application.add_handler(CommandHandler("approve", approve_cmd))
    application.add_handler(CommandHandler("unapprove", unapprove_cmd))
    application.add_handler(CommandHandler("add", add_hours_cmd))
    application.add_handler(CommandHandler("all_users", all_users_cmd))
    application.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # Off keyword command via regex: /off_<keyword>
    application.add_handler(MessageHandler(filters.Regex(r"^/off_.+"), off_keyword_cmd))

    # Buttons
    application.add_handler(CallbackQueryHandler(button_handler))

    logger.info("Main Bot is running...")
    application.run_polling()

if __name__ == "__main__":
    main()
