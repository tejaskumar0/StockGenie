from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
import yfinance as yf
from apscheduler.schedulers.asyncio import AsyncIOScheduler
import nest_asyncio
from dotenv import load_dotenv
import os
from supabase import create_client, Client
from datetime import datetime, timedelta
import pytz
import asyncio

load_dotenv()

# === Supabase Setup ===
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

market_alert_jobs = {}

def parse_interval(time_str):
    if 'hour' in time_str:
        return int(time_str.replace("hour", "")) * 60
    elif 'min' in time_str:
        return int(time_str.replace("min", ""))
    return None

def is_market_open():
    eastern = pytz.timezone("US/Eastern")
    now = datetime.now(eastern)
    return now.weekday() < 5 and (now.hour > 9 or (now.hour == 9 and now.minute >= 30)) and now.hour < 16

def send_market_update(app, user_id):
    response = supabase.table("user_stocks").select("ticker").eq("user_id", user_id).execute()
    if not response.data:
        return
    message = "📈 Market Update:\n"
    for item in response.data:
        ticker = item["ticker"]
        price = get_stock_price(ticker)
        if price:
            message += f"• {ticker}: ${price:.2f}\n"
        else:
            message += f"• {ticker}: Price not available\n"
    app.bot.send_message(chat_id=user_id, text=message)



# Patch the event loop
nest_asyncio.apply()

# Get the latest closing price for a stock
def get_stock_price(ticker):
    try:
        stock = yf.Ticker(ticker)
        data = stock.history(period="1d")
        return data['Close'].iloc[-1] if not data.empty else None
    except Exception:
        return None

# /start command
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome to *StockGenie* — your personal Telegram stock assistant, created by Tejas Kumar!\n\n"
        "Here’s what I can help you with:\n"
        "📈 /add <ticker> – Start tracking a stock (e.g., /add AAPL)\n"
        "🗑️ /delete <ticker> – Remove a stock from your watchlist\n"
        "📋 /list – View all stocks you’re currently tracking\n"
        "🔎 /check <ticker> – Check the current price of any stock\n"
        "📤 Daily Alerts – Sent daily at 10PM SG time\n"
        "🕒 /marketalert <time> – Get price updates every X mins/hours during US market hours (e.g. /marketalert 1hour)\n"
        "🔕 /mute – Turn off all alerts\n"
        "🔔 /unmute – Turn them back on\n"
        "ℹ️ /status – Check if alerts are currently active\n\n"
        "Let’s get started — try /add TSLA to begin! 🚀",
        parse_mode="Markdown"
    )


# /add command
async def add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Please provide a stock ticker. Example: /add TSLA")
        return
    ticker = context.args[0].upper()
    price = get_stock_price(ticker)
    if price is None:
        await update.message.reply_text("Couldn't fetch stock data. Check the ticker symbol.")
        return
    response = supabase.table("user_stocks").select("*").eq("user_id", user_id).eq("ticker", ticker).execute()
    if response.data:
        await update.message.reply_text(f"You're already tracking {ticker}. Current price: ${price:.2f}")
        return
    supabase.table("user_stocks").insert({"user_id": user_id, "ticker": ticker}).execute()
    prefs = supabase.table("user_preferences").select("*").eq("user_id", user_id).execute()
    if not prefs.data:
        supabase.table("user_preferences").insert({"user_id": user_id, "alert_enabled": True}).execute()
    await update.message.reply_text(f"✅ Now tracking {ticker} at ${price:.2f}")

# /delete command
async def del_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    if not context.args:
        await update.message.reply_text("Please provide a stock ticker. Example: /delete TSLA")
        return
    ticker = context.args[0].upper()
    response = supabase.table("user_stocks") \
        .delete() \
        .eq("user_id", user_id) \
        .eq("ticker", ticker) \
        .execute()
    if response.data:
        await update.message.reply_text(f"🗑️ {ticker} has been removed from your tracked stocks.")
    else:
        await update.message.reply_text("You're not tracking this stock.")

# /check command
async def check_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Please provide a stock ticker. Example: /check TSLA")
        return
    ticker = context.args[0].upper()
    price = get_stock_price(ticker)
    if price is None:
        await update.message.reply_text(f"Couldn't fetch price for {ticker}. Check the ticker symbol.")
        return
    await update.message.reply_text(f" Current price of {ticker} is ${price:.2f}")

# /list command
async def list_stocks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    response = supabase.table("user_stocks").select("ticker").eq("user_id", user_id).execute()
    if not response.data:
        await update.message.reply_text("📭 You're not tracking any stocks. Use /add <ticker> to get started.")
        return
    response_text = "📋 Your tracked stocks:\n"
    for item in response.data:
        ticker = item["ticker"]
        price = get_stock_price(ticker)
        if price:
            response_text += f"• {ticker}: ${price:.2f}\n"
        else:
            response_text += f"• {ticker}: Price not available\n"
    await update.message.reply_text(response_text)

async def mute_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id

    # Disable daily alerts
    supabase.table("user_preferences").upsert({"user_id": user_id, "alert_enabled": False}).execute()

    # Cancel market alert job if exists
    job_id = market_alert_jobs.get(user_id)
    if job_id:
        scheduler.remove_job(job_id)
        del market_alert_jobs[user_id]

    await update.message.reply_text("🔕 All alerts muted. You will no longer receive daily or market hour stock updates.")

async def unmute_alerts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    supabase.table("user_preferences").upsert({"user_id": user_id, "alert_enabled": True}).execute()
    await update.message.reply_text("🔔 Daily stock alerts have been re-enabled.")

# /status command
async def alert_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    result = supabase.table("user_preferences").select("alert_enabled, market_alert_enabled").eq("user_id", user_id).execute()

    if result.data:
        daily = result.data[0].get("alert_enabled", False)
        market = result.data[0].get("market_alert_enabled", False)

        msg = "🔔 *Alert Status:*\n"
        msg += f"• Daily Alerts: {'✅ On' if daily else '❌ Off'}\n"
        msg += f"• Market Alerts: {'✅ On' if market else '❌ Off'}"

        await update.message.reply_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text("ℹ️ You have no alert preferences set.")


# /help command
async def help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Here’s what I can do for you:\n"
        "📈 /add <ticker> – Start tracking a stock (e.g., /add AAPL)\n"
        "🗑️ /delete <ticker> – Remove a stock from your watchlist\n"
        "📋 /list – View all stocks you’re currently tracking\n"
        "🔎 /check <ticker> – Check the current price of any stock\n"
        "📤 Daily Alerts – Sent daily at 10PM SG time\n"
        "🕒 /marketalert <time> – Get price updates every X mins/hours during US market hours (e.g. /marketalert 1hour)\n"
        "🔕 /mute – Turn off all alerts\n"
        "🔔 /unmute – Turn them back on\n"
        "ℹ️ /status – Check if alerts are currently active\n\n"
        "Let’s get started — try /add TSLA to begin! 🚀",
    )

# Unknown command fallback
async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❓ That command doesn't exist. Here's what I can help you with:")
    await help(update, context)

# Daily alerts
scheduler = AsyncIOScheduler()


async def send_alerts(app):
    sg_tz = pytz.timezone("Asia/Singapore")
    today = datetime.now(sg_tz).strftime("%A, %B %d")

    # Get users with alerts enabled
    prefs_res = supabase.table("user_preferences").select("*").eq("alert_enabled", True).execute()
    enabled_users = [row["user_id"] for row in prefs_res.data]

    # Get stocks for those users
    response = supabase.table("user_stocks").select("*").in_("user_id", enabled_users).execute()

    user_data = {}
    for record in response.data:
        user_id = record["user_id"]
        ticker = record["ticker"]
        user_data.setdefault(user_id, []).append(ticker)

    for user_id, tickers in user_data.items():
        message = f"🌙 Good evening!\n📅 *{today}*\nHere’s your stock update for today:\n\n"
        for ticker in tickers:
            price = get_stock_price(ticker)
            if price:
                message += f"• {ticker}: ${price:.2f}\n"
            else:
                message += f"• {ticker}: Price not available\n"

        # 🔁 Fix: Schedule message as coroutine
        await app.bot.send_message(chat_id=user_id, text=message, parse_mode="Markdown")


async def market_alert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id

    if not context.args:
        await update.message.reply_text("Please specify a time interval. Example: /MarketAlert 1hour or /MarketAlert 0 to cancel.")
        return

    interval_str = context.args[0].lower()

    # 🔕 Cancel alert
    if interval_str == "0":
        job_id = market_alert_jobs.get(user_id)
        if job_id:
            scheduler.remove_job(job_id)
            del market_alert_jobs[user_id]

        # Turn off in Supabase
        supabase.table("user_preferences").upsert({
            "user_id": user_id,
            "market_alert_enabled": False
        }).execute()

        await update.message.reply_text("🛑 Market alerts have been cancelled.")
        return

    # 🔁 Parse interval
    interval = parse_interval(interval_str)
    if not interval:
        await update.message.reply_text("Invalid format. Use '1hour', '30min', or '0' to cancel.")
        return

    if user_id in market_alert_jobs:
        await update.message.reply_text("⏱️ You already have a Market Alert running. Use /MarketAlert 0 or /mute to disable.")
        return

    # ⚠️ Warn if market is closed
    eastern = pytz.timezone("US/Eastern")
    now = datetime.now(eastern)
    if now.weekday() >= 5 or now.hour < 9 or (now.hour == 9 and now.minute < 30) or now.hour >= 16:
        await update.message.reply_text(
            "⚠️ Heads up! The market is currently closed. Alerts will only be sent during market hours (Mon–Fri, 9:30AM–4PM ET)."
        )

    # ✅ Schedule the alert
    def job():
        if is_market_open():
            send_market_update(context.application, user_id)

    job_id = f"market_alert_{user_id}"
    scheduler.add_job(job, 'interval', minutes=interval, id=job_id)
    market_alert_jobs[user_id] = job_id

    # Enable in Supabase
    supabase.table("user_preferences").upsert({
        "user_id": user_id,
        "market_alert_enabled": True
    }).execute()

    await update.message.reply_text(f"✅ Market alerts set every {interval} minutes during market hours (Mon–Fri, 9:30AM–4PM ET).")


# Main app
async def main():
    TOKEN = os.getenv("BOT_TOKEN")
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help))
    app.add_handler(CommandHandler("add", add_stock))
    app.add_handler(CommandHandler("delete", del_stock))
    app.add_handler(CommandHandler("list", list_stocks))
    app.add_handler(CommandHandler("check", check_stock))
    app.add_handler(CommandHandler("mute", mute_alerts))
    app.add_handler(CommandHandler("unmute", unmute_alerts))
    app.add_handler(CommandHandler("status", alert_status))
    app.add_handler(CommandHandler("marketalert", market_alert))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    scheduler.start()

    scheduler.add_job(lambda: asyncio.create_task(send_alerts(app)), 'cron', hour=15, minute=0)

    print("✅ StockGenie is running...")
    await app.run_polling()

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())



