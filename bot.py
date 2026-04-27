#!/usr/bin/env python3
"""
HGBot - Hemp Dropshipping Telegram Bot
Accepts BTC and SOL payments, notifies owner via Telegram
"""

import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ============================================================
#  OWNER CONFIGURATION — FILL THESE IN
# ============================================================
BOT_TOKEN = "8720432686:AAHOqXHCxD7oqxaWwCZ0RY4S2u63_UVPNRA"         # From @BotFather
OWNER_CHAT_ID = "1728792420"   # Your personal Telegram ID

# Your crypto wallet addresses
BTC_WALLET = "bc1qp0rg0jy6tfyvh9ykfefkln49g2lspgf5rpwfys"
SOL_WALLET = "YOUR_SOL_WALLET_ADDRESS"

# ============================================================
#  PRODUCT MENU — EDIT THIS ANYTIME TO ADD/REMOVE PRODUCTS
#  Format: {"name": "Product Name", "price": 00.00, "description": "Short description"}
# ============================================================
PRODUCTS = [
    {"id": 1, "name": "CBD Tincture 1000mg", "price": 45.00, "description": "Full spectrum, 30ml bottle"},
    {"id": 2, "name": "CBD Gummies 25mg", "price": 30.00, "description": "30 count, assorted flavors"},
    {"id": 3, "name": "Hemp Flower - OG Kush", "price": 25.00, "description": "3.5g, indoor grown"},
    {"id": 4, "name": "CBD Topical Cream", "price": 35.00, "description": "500mg, 2oz jar"},
    {"id": 5, "name": "Delta-8 Vape Cart", "price": 40.00, "description": "1g, various strains"},
]

# ============================================================
#  CONVERSATION STATES
# ============================================================
(
    BROWSING,
    SELECTING_QTY,
    COLLECTING_NAME,
    COLLECTING_ADDRESS,
    COLLECTING_CITY,
    COLLECTING_STATE,
    COLLECTING_ZIP,
    SELECTING_CRYPTO,
    CONFIRMING_PAYMENT,
) = range(9)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================
#  HELPER FUNCTIONS
# ============================================================

def build_product_menu():
    keyboard = []
    for p in PRODUCTS:
        keyboard.append([InlineKeyboardButton(
            f"{p['name']} — ${p['price']:.2f}",
            callback_data=f"product_{p['id']}"
        )])
    keyboard.append([InlineKeyboardButton("🛒 View Cart", callback_data="view_cart")])
    return InlineKeyboardMarkup(keyboard)


def get_product_by_id(product_id):
    return next((p for p in PRODUCTS if p["id"] == product_id), None)


def format_order_for_owner(order_data):
    product = order_data.get("product", {})
    crypto = order_data.get("crypto", "")
    wallet = BTC_WALLET if crypto == "BTC" else SOL_WALLET

    msg = (
        f"🛍️ NEW ORDER — HGBot\n"
        f"{'='*30}\n"
        f"📦 Product: {product.get('name')}\n"
        f"   Qty: {order_data.get('qty', 1)}\n"
        f"   Price: ${product.get('price', 0) * order_data.get('qty', 1):.2f}\n\n"
        f"👤 Customer Info:\n"
        f"   Name: {order_data.get('name')}\n"
        f"   Address: {order_data.get('address')}\n"
        f"   City: {order_data.get('city')}\n"
        f"   State: {order_data.get('state')}\n"
        f"   ZIP: {order_data.get('zip')}\n\n"
        f"💰 Payment:\n"
        f"   Method: {crypto}\n"
        f"   Wallet Used: {wallet}\n"
        f"   Customer confirmed payment sent ✅\n"
        f"{'='*30}\n"
        f"⚡ Action: Forward this order to your supplier!"
    )
    return msg


# ============================================================
#  COMMAND HANDLERS
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        f"👋 Welcome to *HGBot*!\n\n"
        f"Your premium hemp products shop.\n"
        f"Browse our menu below, place your order, and pay with crypto — simple and private.\n\n"
        f"👇 Select a product to get started:",
        parse_mode="Markdown",
        reply_markup=build_product_menu()
    )
    return BROWSING


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 *How to Order:*\n\n"
        "1. Browse the menu with /start\n"
        "2. Pick your product\n"
        "3. Enter your shipping info\n"
        "4. Pay with BTC or SOL\n"
        "5. Confirm and you're done!\n\n"
        "Questions? Contact the shop admin.",
        parse_mode="Markdown"
    )


# ============================================================
#  PRODUCT SELECTION
# ============================================================

async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    product_id = int(query.data.split("_")[1])
    product = get_product_by_id(product_id)

    if not product:
        await query.edit_message_text("❌ Product not found. Try /start again.")
        return BROWSING

    context.user_data["product"] = product

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("1", callback_data="qty_1"),
            InlineKeyboardButton("2", callback_data="qty_2"),
            InlineKeyboardButton("3", callback_data="qty_3"),
        ],
        [
            InlineKeyboardButton("4", callback_data="qty_4"),
            InlineKeyboardButton("5", callback_data="qty_5"),
            InlineKeyboardButton("Other", callback_data="qty_other"),
        ],
        [InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_menu")]
    ])

    await query.edit_message_text(
        f"*{product['name']}*\n"
        f"_{product['description']}_\n"
        f"💵 ${product['price']:.2f} each\n\n"
        f"How many would you like?",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    return SELECTING_QTY


async def qty_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "qty_other":
        await query.edit_message_text("How many would you like? Type a number:")
        return SELECTING_QTY

    qty = int(query.data.split("_")[1])
    return await process_qty(update, context, qty, is_query=True)


async def qty_typed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        qty = int(update.message.text.strip())
        if qty < 1 or qty > 99:
            await update.message.reply_text("Please enter a number between 1 and 99.")
            return SELECTING_QTY
    except ValueError:
        await update.message.reply_text("Please enter a valid number.")
        return SELECTING_QTY

    return await process_qty(update, context, qty, is_query=False)


async def process_qty(update, context, qty, is_query):
    context.user_data["qty"] = qty
    product = context.user_data["product"]
    total = product["price"] * qty

    msg = (
        f"✅ *{qty}x {product['name']}*\n"
        f"Total: *${total:.2f}*\n\n"
        f"Now I need your shipping info.\n\n"
        f"📝 What's your *full name*?"
    )

    if is_query:
        await update.callback_query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")

    return COLLECTING_NAME


# ============================================================
#  SHIPPING INFO COLLECTION
# ============================================================

async def collect_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("📬 What's your *street address*?", parse_mode="Markdown")
    return COLLECTING_ADDRESS


async def collect_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["address"] = update.message.text.strip()
    await update.message.reply_text("🏙️ What *city* do you live in?", parse_mode="Markdown")
    return COLLECTING_CITY


async def collect_city(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["city"] = update.message.text.strip()
    await update.message.reply_text("🗺️ What *state*? (e.g. TX, CA, FL)", parse_mode="Markdown")
    return COLLECTING_STATE


async def collect_state(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = update.message.text.strip().upper()
    await update.message.reply_text("📮 What's your *ZIP code*?", parse_mode="Markdown")
    return COLLECTING_ZIP


async def collect_zip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["zip"] = update.message.text.strip()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("₿ Bitcoin (BTC)", callback_data="crypto_BTC"),
            InlineKeyboardButton("◎ Solana (SOL)", callback_data="crypto_SOL"),
        ]
    ])

    await update.message.reply_text(
        "💰 Almost done! Which crypto will you pay with?",
        reply_markup=keyboard
    )
    return SELECTING_CRYPTO


# ============================================================
#  PAYMENT
# ============================================================

async def crypto_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    crypto = query.data.split("_")[1]
    context.user_data["crypto"] = crypto

    product = context.user_data["product"]
    qty = context.user_data["qty"]
    total = product["price"] * qty
    wallet = BTC_WALLET if crypto == "BTC" else SOL_WALLET

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ I've Sent Payment", callback_data="payment_sent")],
        [InlineKeyboardButton("❌ Cancel Order", callback_data="cancel_order")]
    ])

    await query.edit_message_text(
        f"💳 *Payment Instructions*\n\n"
        f"Send exactly *${total:.2f} USD* worth of *{crypto}* to:\n\n"
        f"`{wallet}`\n\n"
        f"_(Tap the address to copy it)_\n\n"
        f"⚠️ Send the exact amount. Once sent, tap the button below.",
        parse_mode="Markdown",
        reply_markup=keyboard
    )
    return CONFIRMING_PAYMENT


async def payment_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    order_data = context.user_data
    order_summary = format_order_for_owner(order_data)

    # Notify the owner
    try:
        await context.bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=order_summary
        )
    except Exception as e:
        logger.error(f"Failed to notify owner: {e}")

    # Confirm to customer
    product = order_data["product"]
    qty = order_data["qty"]
    total = product["price"] * qty

    await query.edit_message_text(
        f"🎉 *Order Confirmed!*\n\n"
        f"Thank you! Your order has been received.\n\n"
        f"📦 *{qty}x {product['name']}*\n"
        f"💰 ${total:.2f} in {order_data['crypto']}\n"
        f"📬 Shipping to: {order_data['name']}, {order_data['city']}, {order_data['state']}\n\n"
        f"Your package will be on its way soon!\n"
        f"Type /start to place another order.",
        parse_mode="Markdown"
    )

    context.user_data.clear()
    return ConversationHandler.END


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text(
        "❌ Order cancelled. Type /start to begin a new order."
    )
    return ConversationHandler.END


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "👇 Select a product:",
        reply_markup=build_product_menu()
    )
    return BROWSING


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Order cancelled. Type /start to begin again.")
    return ConversationHandler.END


# ============================================================
#  MAIN
# ============================================================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            BROWSING: [
                CallbackQueryHandler(product_selected, pattern="^product_"),
                CallbackQueryHandler(back_to_menu, pattern="^back_menu$"),
            ],
            SELECTING_QTY: [
                CallbackQueryHandler(qty_selected, pattern="^qty_"),
                CallbackQueryHandler(back_to_menu, pattern="^back_menu$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, qty_typed),
            ],
            COLLECTING_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_name)],
            COLLECTING_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_address)],
            COLLECTING_CITY: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_city)],
            COLLECTING_STATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_state)],
            COLLECTING_ZIP: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_zip)],
            SELECTING_CRYPTO: [CallbackQueryHandler(crypto_selected, pattern="^crypto_")],
            CONFIRMING_PAYMENT: [
                CallbackQueryHandler(payment_confirmed, pattern="^payment_sent$"),
                CallbackQueryHandler(cancel_order, pattern="^cancel_order$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("help", help_command))

    print("🌿 HGBot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
