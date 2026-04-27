import os
import asyncio
import aiohttp
import logging
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================
#  CONFIG
# ============================================================

BOT_TOKEN      = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
ADMIN_CHAT_ID  = int(os.environ.get("ADMIN_CHAT_ID", "0"))
NOTION_TOKEN   = os.environ.get("NOTION_TOKEN", "YOUR_NOTION_TOKEN")
NOTION_DB_ID   = os.environ.get("NOTION_DB_ID", "a49d2679-9703-4acc-aa26-1e234404512f")
BTC_ADDRESS    = os.environ.get("BTC_ADDRESS", "bc1qk257zk8wd7l5ls26psnvvpdmnssqzq926l4m09")
SOL_ADDRESS    = os.environ.get("SOL_ADDRESS", "GWtFxEg86bksPe5tPqHoy4h5GjufWi3UjrNd7tN1JwYc")

# ============================================================
#  PRODUCTS — loaded live from Notion
# ============================================================

NOTION_PRODUCTS_DB_ID = os.environ.get("NOTION_PRODUCTS_DB_ID", "d18aa91f-bd1b-4bf3-b15f-7f885b19f32d")

async def fetch_products_from_notion() -> dict:
    """Pull active products from the HGBot Products Notion database."""
    url = f"https://api.notion.com/v1/databases/{NOTION_PRODUCTS_DB_ID}/query"
    payload = {
        "filter": {"property": "Active", "checkbox": {"equals": True}},
        "sorts": [{"property": "Product Name", "direction": "ascending"}]
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=notion_headers()) as resp:
                data = await resp.json()

        products = {}
        for i, page in enumerate(data.get("results", [])):
            props = page["properties"]
            name  = props["Product Name"]["title"][0]["text"]["content"] if props["Product Name"]["title"] else "Unknown"
            price = props["Price (USD)"]["number"] or 0.0
            pid   = f"p{i+1}"
            products[pid] = {"name": name, "price": price}
        return products

    except Exception as e:
        logger.error(f"Failed to fetch products from Notion: {e}")
        # Fallback hardcoded list if Notion is unreachable
        return {
            "p1": {"name": "CBD Gummies 25mg (30ct)",  "price": 39.99},
            "p2": {"name": "CBD Tincture 1000mg",       "price": 54.99},
            "p3": {"name": "CBD Topical Cream 500mg",   "price": 34.99},
            "p4": {"name": "Hemp Flower 3.5g",          "price": 24.99},
            "p5": {"name": "Delta-8 Gummies (20ct)",    "price": 29.99},
            "p6": {"name": "CBD Capsules 25mg (60ct)",  "price": 44.99},
        }

# ============================================================
#  STATES
# ============================================================

(BROWSING, SELECTING_QTY, COLLECTING_NAME, COLLECTING_ADDRESS,
 COLLECTING_CITY, COLLECTING_STATE, COLLECTING_ZIP,
 SELECTING_CRYPTO) = range(8)

_order_counter = 1000

def next_order_id():
    global _order_counter
    _order_counter += 1
    return f"HG-{_order_counter}"

# ============================================================
#  CRYPTO PRICE
# ============================================================

async def get_crypto_price(symbol: str) -> float:
    ids = {"BTC": "bitcoin", "SOL": "solana"}
    coin_id = ids.get(symbol.upper(), "bitcoin")
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                data = await resp.json()
                return float(data[coin_id]["usd"])
    except Exception as e:
        logger.error(f"Price fetch error: {e}")
        return None

def usd_to_crypto(usd_amount: float, crypto_price: float) -> float:
    return round(usd_amount / crypto_price, 8)

# ============================================================
#  BLOCKCHAIN WATCHERS
# ============================================================

async def wait_for_btc_payment(app, chat_id, order_id, expected_btc, usd_amount, order_data):
    url = f"https://api.blockcypher.com/v1/btc/main/addrs/{BTC_ADDRESS}/full?limit=5"
    seen_txids = set()

    # Snapshot existing txids
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
                for tx in data.get("txs", []):
                    seen_txids.add(tx["hash"])
    except Exception:
        pass

    deadline = asyncio.get_event_loop().time() + 3600

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(30)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    data = await resp.json()

            for tx in data.get("txs", []):
                txid = tx["hash"]
                if txid in seen_txids:
                    continue
                seen_txids.add(txid)

                received_satoshis = sum(
                    o.get("value", 0) for o in tx.get("outputs", [])
                    if BTC_ADDRESS in o.get("addresses", [])
                )

                if received_satoshis > 0:
                    received_btc   = received_satoshis / 1e8
                    detected_time  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

                    # ✅ Auto-confirm to customer
                    await app.bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"✅ *Payment confirmed — sending order details to vendor!*\n\n"
                            f"Order: *#{order_id}*\n"
                            f"Amount: *{received_btc:.8f} BTC*\n"
                            f"Time: {detected_time}\n\n"
                            f"Your package will be on its way soon. Thank you! 🌿"
                        ),
                        parse_mode="Markdown"
                    )

                    await update_notion_payment_detected(order_id, received_btc, detected_time)

                    # Notify admin with pre-formatted vendor message
                    vendor_msg = format_vendor_message(order_data, order_id, received_btc, usd_amount, detected_time)
                    keyboard = InlineKeyboardMarkup([[
                        InlineKeyboardButton(
                            "✅ Mark as Forwarded to Vendor",
                            callback_data=f"forward_{order_id}_{chat_id}"
                        )
                    ]])

                    await app.bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=(
                            f"💰 *PAYMENT RECEIVED — Order #{order_id}*\n\n"
                            f"🕐 {detected_time}\n"
                            f"₿ {received_btc:.8f} BTC (${usd_amount:.2f})\n\n"
                            f"👤 {order_data['name']}\n"
                            f"💬 Telegram: {order_data.get('telegram_username', 'Unknown')}\n"
                            f"📬 {order_data['address']}, {order_data['city']}, "
                            f"{order_data['state']} {order_data['zip']}\n"
                            f"📦 {order_data['qty']}x {order_data['product']['name']}\n\n"
                            f"*Copy & send to vendor 👇*\n\n"
                            f"`{vendor_msg}`\n\n"
                            f"Press the button once you've messaged the vendor:"
                        ),
                        parse_mode="Markdown",
                        reply_markup=keyboard
                    )
                    return

        except Exception as e:
            logger.error(f"BTC polling error: {e}")

    # 1 hour timeout
    await app.bot.send_message(
        chat_id=chat_id,
        text=(
            "⚠️ We haven't detected a payment for your order after 1 hour.\n\n"
            "If you already sent crypto, please contact support with your transaction ID.\n"
            "Type /start to place a new order."
        )
    )


async def wait_for_sol_payment(app, chat_id, order_id, expected_sol, usd_amount, order_data):
    rpc_url = "https://api.mainnet-beta.solana.com"
    seen_sigs = set()

    async def get_sigs():
        payload = {"jsonrpc": "2.0", "id": 1,
                   "method": "getSignaturesForAddress",
                   "params": [SOL_ADDRESS, {"limit": 5}]}
        async with aiohttp.ClientSession() as session:
            async with session.post(rpc_url, json=payload) as resp:
                data = await resp.json()
                return [r["signature"] for r in data.get("result", [])]

    try:
        for sig in await get_sigs():
            seen_sigs.add(sig)
    except Exception:
        pass

    deadline = asyncio.get_event_loop().time() + 3600

    while asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(30)
        try:
            sigs = await get_sigs()
            for sig in sigs:
                if sig in seen_sigs:
                    continue
                seen_sigs.add(sig)

                detected_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

                # ✅ Auto-confirm to customer
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"✅ *Payment confirmed — sending order details to vendor!*\n\n"
                        f"Order: *#{order_id}*\n"
                        f"Time: {detected_time}\n\n"
                        f"Your package will be on its way soon. Thank you! 🌿"
                    ),
                    parse_mode="Markdown"
                )

                await update_notion_payment_detected(order_id, expected_sol, detected_time)

                vendor_msg = format_vendor_message(order_data, order_id, expected_sol, usd_amount, detected_time)
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        "✅ Mark as Forwarded to Vendor",
                        callback_data=f"forward_{order_id}_{chat_id}"
                    )
                ]])

                await app.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=(
                        f"💰 *PAYMENT RECEIVED — Order #{order_id}*\n\n"
                        f"🕐 {detected_time}\n"
                        f"◎ SOL TX: `{sig[:20]}...`\n"
                        f"💵 ${usd_amount:.2f}\n\n"
                        f"👤 {order_data['name']}\n"
                        f"💬 Telegram: {order_data.get('telegram_username', 'Unknown')}\n"
                        f"📬 {order_data['address']}, {order_data['city']}, "
                        f"{order_data['state']} {order_data['zip']}\n"
                        f"📦 {order_data['qty']}x {order_data['product']['name']}\n\n"
                        f"*Copy & send to vendor 👇*\n\n"
                        f"`{vendor_msg}`\n\n"
                        f"Press the button once you've messaged the vendor:"
                    ),
                    parse_mode="Markdown",
                    reply_markup=keyboard
                )
                return

        except Exception as e:
            logger.error(f"SOL polling error: {e}")

    await app.bot.send_message(
        chat_id=chat_id,
        text=(
            "⚠️ We haven't detected a payment for your order after 1 hour.\n\n"
            "If you already sent crypto, please contact support with your transaction ID.\n"
            "Type /start to place a new order."
        )
    )

# ============================================================
#  VENDOR MESSAGE
# ============================================================

def format_vendor_message(order_data, order_id, crypto_amount, usd_amount, detected_time):
    return (
        f"NEW ORDER #{order_id}\n"
        f"Date: {detected_time}\n"
        f"---\n"
        f"Product: {order_data['qty']}x {order_data['product']['name']}\n"
        f"Total: ${usd_amount:.2f}\n"
        f"---\n"
        f"Ship To:\n"
        f"{order_data['name']}\n"
        f"{order_data['address']}\n"
        f"{order_data['city']}, {order_data['state']} {order_data['zip']}\n"
        f"---\n"
        f"Payment: {crypto_amount} {order_data['crypto']} - CONFIRMED"
    )

# ============================================================
#  NOTION
# ============================================================

def notion_headers():
    return {
        "Authorization": f"Bearer {NOTION_TOKEN}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

async def create_notion_order(order_data, order_id, usd_amount, crypto_amount, placed_time):
    url = "https://api.notion.com/v1/pages"
    tg_username = order_data.get("telegram_username", "Unknown")
    payload = {
        "parent": {"database_id": NOTION_DB_ID},
        "properties": {
            "Order ID":           {"title": [{"text": {"content": f"#{order_id}"}}]},
            "Date Placed":        {"rich_text": [{"text": {"content": placed_time}}]},
            "Customer Name":      {"rich_text": [{"text": {"content": order_data["name"]}}]},
            "Telegram Username":  {"rich_text": [{"text": {"content": tg_username}}]},
            "Address":            {"rich_text": [{"text": {"content": f"{order_data['address']}, {order_data['city']}, {order_data['state']} {order_data['zip']}"}}]},
            "Product":            {"rich_text": [{"text": {"content": f"{order_data['qty']}x {order_data['product']['name']}"}}]},
            "USD Amount":         {"rich_text": [{"text": {"content": f"${usd_amount:.2f}"}}]},
            "Crypto Amount":      {"rich_text": [{"text": {"content": f"{crypto_amount} {order_data['crypto']}"}}]},
            "Payment Status":     {"select": {"name": "Pending"}},
        }
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=notion_headers()) as resp:
                result = await resp.json()
                return result.get("id")
    except Exception as e:
        logger.error(f"Notion create error: {e}")

async def find_notion_page(order_id):
    url = f"https://api.notion.com/v1/databases/{NOTION_DB_ID}/query"
    payload = {"filter": {"property": "Order ID", "title": {"equals": f"#{order_id}"}}}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=notion_headers()) as resp:
            data = await resp.json()
            results = data.get("results", [])
            return results[0]["id"] if results else None

async def update_notion_payment_detected(order_id, crypto_amount, detected_time):
    try:
        page_id = await find_notion_page(order_id)
        if not page_id:
            return
        url = f"https://api.notion.com/v1/pages/{page_id}"
        payload = {"properties": {
            "Payment Status":   {"select": {"name": "Payment Detected"}},
            "Payment Detected": {"rich_text": [{"text": {"content": detected_time}}]},
        }}
        async with aiohttp.ClientSession() as session:
            await session.patch(url, json=payload, headers=notion_headers())
    except Exception as e:
        logger.error(f"Notion payment update error: {e}")

async def update_notion_forwarded(order_id, forwarded_time):
    try:
        page_id = await find_notion_page(order_id)
        if not page_id:
            return
        url = f"https://api.notion.com/v1/pages/{page_id}"
        payload = {"properties": {
            "Payment Status":      {"select": {"name": "Forwarded to Vendor"}},
            "Forwarded to Vendor": {"rich_text": [{"text": {"content": forwarded_time}}]},
        }}
        async with aiohttp.ClientSession() as session:
            await session.patch(url, json=payload, headers=notion_headers())
    except Exception as e:
        logger.error(f"Notion forwarded update error: {e}")

# ============================================================
#  BOT HANDLERS
# ============================================================

def build_product_menu(products: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{p['name']} — ${p['price']:.2f}", callback_data=f"product_{pid}")]
        for pid, p in products.items()
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    products = await fetch_products_from_notion()
    context.user_data["products"] = products  # cache for this session

    await update.message.reply_text(
        "🌿 *Welcome to HGBot!*\n\nBrowse our hemp products below.\n\n👇 *Select a product:*",
        parse_mode="Markdown", reply_markup=build_product_menu(products)
    )
    return BROWSING

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Type /start to place an order.\nType /cancel to cancel at any time.")

async def testpay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only command to simulate a payment detection."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return  # Silently ignore if not admin

    order_id      = next_order_id()
    detected_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    test_btc      = 0.00042000
    test_usd      = 54.99

    fake_order_data = {
        "name":    "Test Customer",
        "address": "123 Test St",
        "city":    "Austin",
        "state":   "TX",
        "zip":     "78701",
        "qty":     1,
        "crypto":  "BTC",
        "product": {"name": "CBD Tincture 1000mg", "price": 54.99},
    }

    # Log to Notion
    await create_notion_order(fake_order_data, order_id, test_usd, test_btc, detected_time)
    await update_notion_payment_detected(order_id, test_btc, detected_time)

    # Simulate customer confirmation (sends to admin since this is a test)
    await update.message.reply_text(
        f"✅ *[TEST] Payment confirmed — sending order details to vendor!*\n\n"
        f"Order: *#{order_id}*\n"
        f"Amount: *{test_btc:.8f} BTC*\n"
        f"Time: {detected_time}\n\n"
        f"Your package will be on its way soon. Thank you! 🌿",
        parse_mode="Markdown"
    )

    # Simulate admin notification
    vendor_msg = format_vendor_message(fake_order_data, order_id, test_btc, test_usd, detected_time)
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton(
            "✅ Mark as Forwarded to Vendor",
            callback_data=f"forward_{order_id}_{update.effective_chat.id}"
        )
    ]])

    await update.message.reply_text(
        f"💰 *[TEST] PAYMENT RECEIVED — Order #{order_id}*\n\n"
        f"🕐 {detected_time}\n"
        f"₿ {test_btc:.8f} BTC (${test_usd:.2f})\n\n"
        f"👤 {fake_order_data['name']}\n"
        f"📬 {fake_order_data['address']}, {fake_order_data['city']}, "
        f"{fake_order_data['state']} {fake_order_data['zip']}\n"
        f"📦 1x {fake_order_data['product']['name']}\n\n"
        f"*Copy & send to vendor 👇*\n\n"
        f"`{vendor_msg}`\n\n"
        f"Press the button once you've messaged the vendor:",
        parse_mode="Markdown",
        reply_markup=keyboard
    )

async def product_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    pid = query.data.split("_")[1]
    products = context.user_data.get("products", {})
    product = products.get(pid)
    if not product:
        await query.edit_message_text("Sorry, that product is no longer available. Type /start to see the current menu.")
        return ConversationHandler.END
    context.user_data["product"] = product
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("1", callback_data="qty_1"),
         InlineKeyboardButton("2", callback_data="qty_2"),
         InlineKeyboardButton("3", callback_data="qty_3")],
        [InlineKeyboardButton("4", callback_data="qty_4"),
         InlineKeyboardButton("5", callback_data="qty_5"),
         InlineKeyboardButton("Other", callback_data="qty_other")],
        [InlineKeyboardButton("⬅️ Back", callback_data="back_menu")]
    ])
    await query.edit_message_text(
        f"*{product['name']}*\n💵 ${product['price']} each\n\nHow many would you like?",
        parse_mode="Markdown", reply_markup=keyboard
    )
    return SELECTING_QTY

async def qty_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "qty_other":
        await query.edit_message_text("How many would you like? Type a number:")
        return SELECTING_QTY
    return await process_qty(update, context, int(query.data.split("_")[1]), is_query=True)

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
    msg = (f"✅ *{qty}x {product['name']}*\nTotal: *${total:.2f}*\n\n"
           f"Now I need your shipping info.\n\n📝 What's your *full name*?")
    if is_query:
        await update.callback_query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await update.message.reply_text(msg, parse_mode="Markdown")
    return COLLECTING_NAME

async def collect_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("📬 What's your *street address*?", parse_mode="Markdown")
    return COLLECTING_ADDRESS

async def collect_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["address"] = update.message.text.strip()
    await update.message.reply_text("🏙️ What *city*?", parse_mode="Markdown")
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
        [InlineKeyboardButton("₿ Bitcoin (BTC)", callback_data="crypto_BTC")],
        [InlineKeyboardButton("◎ Solana (SOL)",  callback_data="crypto_SOL")],
    ])
    await update.message.reply_text("💳 *Choose your payment method:*",
                                     parse_mode="Markdown", reply_markup=keyboard)
    return SELECTING_CRYPTO

async def crypto_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    crypto = query.data.split("_")[1]
    context.user_data["crypto"] = crypto

    product   = context.user_data["product"]
    qty       = context.user_data["qty"]
    usd_total = round(product["price"] * qty, 2)

    await query.edit_message_text("⏳ Fetching live price...")

    price = await get_crypto_price(crypto)
    if price is None:
        await query.edit_message_text(
            "⚠️ Could not fetch live price right now. Please try again.\nType /start to restart."
        )
        return ConversationHandler.END

    crypto_amount = usd_to_crypto(usd_total, price)
    placed_time   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    order_id      = next_order_id()

    # Capture Telegram username
    user = query.from_user
    if user.username:
        tg_username = f"@{user.username}"
    else:
        tg_username = f"{user.full_name} (ID: {user.id})"

    context.user_data["usd_total"]          = usd_total
    context.user_data["crypto_amount"]      = crypto_amount
    context.user_data["order_id"]           = order_id
    context.user_data["placed_time"]        = placed_time
    context.user_data["telegram_username"]  = tg_username

    # Log to Notion immediately
    await create_notion_order(context.user_data, order_id, usd_total, crypto_amount, placed_time)

    wallet = BTC_ADDRESS if crypto == "BTC" else SOL_ADDRESS
    symbol = "₿" if crypto == "BTC" else "◎"

    # Show order summary + wallet — NO button, bot watches automatically
    await query.edit_message_text(
        f"🛒 *Order Summary — #{order_id}*\n"
        f"📅 {placed_time}\n\n"
        f"📦 {qty}x {product['name']}\n"
        f"💵 USD Total: *${usd_total:.2f}*\n"
        f"💱 1 {crypto} = ${price:,.2f}\n"
        f"{symbol} Send exactly: *{crypto_amount} {crypto}*\n\n"
        f"📤 *Send to this address:*\n"
        f"`{wallet}`\n\n"
        f"⏳ Send the exact amount above and we'll detect your payment automatically. "
        f"You'll receive a confirmation message here as soon as it's received!",
        parse_mode="Markdown"
    )

    # Start blockchain watcher in background immediately
    app        = context.application
    order_data = context.user_data.copy()
    chat_id    = query.message.chat_id

    if crypto == "BTC":
        asyncio.create_task(
            wait_for_btc_payment(app, chat_id, order_id, crypto_amount, usd_total, order_data)
        )
    else:
        asyncio.create_task(
            wait_for_sol_payment(app, chat_id, order_id, crypto_amount, usd_total, order_data)
        )

    context.user_data.clear()
    return ConversationHandler.END


async def forward_confirmed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query            = update.callback_query
    await query.answer()
    parts            = query.data.split("_")
    order_id         = parts[1]
    customer_chat_id = int(parts[2])
    forwarded_time   = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    await update_notion_forwarded(order_id, forwarded_time)

    await context.bot.send_message(
        chat_id=customer_chat_id,
        text=(
            f"📦 *Your order is on its way!*\n\n"
            f"Order *#{order_id}* has been forwarded to our fulfillment team.\n"
            f"⏰ {forwarded_time}\n\n"
            f"Expect your package within the standard shipping window. 🌿\n\n"
            f"Type /start to place another order anytime!"
        ),
        parse_mode="Markdown"
    )

    await query.edit_message_text(
        query.message.text + f"\n\n✅ *FORWARDED at {forwarded_time}*",
        parse_mode="Markdown"
    )


async def cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("❌ Order cancelled. Type /start to begin a new order.")
    return ConversationHandler.END

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    products = context.user_data.get("products", {})
    await query.edit_message_text("👇 Select a product:", reply_markup=build_product_menu(products))
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

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            BROWSING:           [CallbackQueryHandler(product_selected, pattern="^product_"),
                                 CallbackQueryHandler(back_to_menu, pattern="^back_menu$")],
            SELECTING_QTY:      [CallbackQueryHandler(qty_selected, pattern="^qty_"),
                                 CallbackQueryHandler(back_to_menu, pattern="^back_menu$"),
                                 MessageHandler(filters.TEXT & ~filters.COMMAND, qty_typed)],
            COLLECTING_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_name)],
            COLLECTING_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_address)],
            COLLECTING_CITY:    [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_city)],
            COLLECTING_STATE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_state)],
            COLLECTING_ZIP:     [MessageHandler(filters.TEXT & ~filters.COMMAND, collect_zip)],
            SELECTING_CRYPTO:   [CallbackQueryHandler(crypto_selected, pattern="^crypto_")],
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("testpay", testpay))
    app.add_handler(CallbackQueryHandler(forward_confirmed, pattern="^forward_"))

    print("🌿 HGBot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
