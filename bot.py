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

def sanitize(text: str) -> str:
    """Clean up text from Notion — strips bold, handles line breaks, fixes escaped chars."""
    import re
    if not text:
        return ""
    text = text.strip()
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = text.replace("<br>", " | ")
    text = text.replace("\\$", "$")
    text = text.replace("\\/", "/")
    text = re.sub(r'\s*\|\s*', ' | ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    return text.strip(" |")

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

            raw_name = props["Product Name"]["title"][0]["text"]["content"] if props["Product Name"]["title"] else "Unknown"
            name = sanitize(raw_name)
            price = props["Price (USD)"]["number"] or 0.0

            strains_blocks = props.get("Strains", {}).get("rich_text", [])
            strains = sanitize(" ".join(b["text"]["content"] for b in strains_blocks))

            tiers_blocks = props.get("Price Tiers", {}).get("rich_text", [])
            price_tiers = sanitize(" ".join(b["text"]["content"] for b in tiers_blocks))

            image_files = props.get("Image", {}).get("files", [])
            image_url = ""
            if image_files:
                f = image_files[0]
                if f.get("type") == "file":
                    image_url = f.get("file", {}).get("url", "")
                elif f.get("type") == "external":
                    image_url = f.get("external", {}).get("url", "")

            pid = f"p{i+1}"
            products[pid] = {
                "name":        name,
                "price":       price,
                "strains":     strains,
                "price_tiers": price_tiers,
                "image_url":   image_url,
            }
        return products

    except Exception as e:
        logger.error(f"Failed to fetch products from Notion: {e}")
        return {}

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
        "🌿 *Welcome to Hg's Stash! 🌿*\n\nBrowse our hemp products below.\n\n👇 *Select a product:*",
        parse_mode="Markdown", reply_markup=build_product_menu(products)
    )
    return BROWSING

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Type /start to place an order.\nType /cancel to cancel at any time.")

async def syncmenu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin only. Forward any product message from the vendor bot to your bot.
    The bot will parse the product name and price and add/update it in Notion.
    Usage: just forward the vendor bot message to your bot.
    Or type /syncmenu to get instructions.
    """
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return

    await update.message.reply_text(
        "📋 *Menu Sync Mode*\n\n"
        "Just *forward any product message* from your vendor bot directly to me!\n\n"
        "I'll automatically parse the product name and price and add it to your Notion menu.\n\n"
        "You can forward multiple messages one at a time.\n\n"
        "To *remove a product*, go to your Notion Products database and uncheck the Active box.",
        parse_mode="Markdown"
    )


def parse_vendor_message(text: str) -> dict:
    """
    Parse a forwarded vendor bot message.
    Extracts: product name (first descriptive line), strains (ALL CAPS lines),
    price tiers (lines like 1oz—$95 or button-style text), and lowest price.
    """
    import re
    lines = [l.strip() for l in text.strip().split("\n") if l.strip()]

    strains = []
    price_tiers = []
    description_lines = []

    # Regex patterns
    strain_re  = re.compile(r'^[A-Z0-9][A-Z0-9 &\'"/x×\-\.]{2,}$')
    # Matches: 1oz—$95, 1oz-$95, 1 oz - $95, 1oz $95, 1lb—$750
    price_re   = re.compile(r'(\d+\s*(?:oz|lb|g))\s*[—\-–]\s*\$(\d+(?:\.\d{1,2})?)', re.IGNORECASE)
    # Also handle button style: "1oz—$9500" (cents encoded) or plain "$95"
    any_price_re = re.compile(r'\$(\d+(?:\.\d{1,2})?)')

    for line in lines:
        # Skip navigation lines like "Return to..." or "Make sure..."
        if any(skip in line for skip in ["Return to", "Make sure", "If you'd like", "This product", "/v_", "/m_", "@"]):
            continue

        price_match = price_re.search(line)
        if price_match:
            size  = price_match.group(1).strip()
            price = price_match.group(2)
            price_tiers.append(f"{size}—${price}")
            continue

        # ALL CAPS strain names (skip short words like "OZ", "LB")
        if strain_re.match(line) and len(line) > 3 and not any_price_re.search(line):
            # Strip any inline links (/p_xxx style)
            clean = re.sub(r'/p_\S+', '', line).strip()
            if clean:
                strains.append(clean)
            continue

        # Everything else that's not a slash command is description
        if not line.startswith("/"):
            description_lines.append(line)

    # Product name = first description line, truncated
    product_name = description_lines[0][:80] if description_lines else "Unknown Product"

    # Lowest price for the Price (USD) field
    lowest_price = None
    for tier in price_tiers:
        m = re.search(r'\$(\d+(?:\.\d{1,2})?)', tier)
        if m:
            p = float(m.group(1))
            if lowest_price is None or p < lowest_price:
                lowest_price = p

    # Fallback: find any $ amount in text
    if lowest_price is None:
        m = any_price_re.search(text)
        if m:
            lowest_price = float(m.group(1))

    return {
        "product_name": product_name,
        "strains":      strains,
        "price_tiers":  price_tiers,
        "lowest_price": lowest_price or 0.0,
    }


async def handle_forwarded_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Parse a forwarded vendor bot message and add product to Notion."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return

    text = update.message.text or update.message.caption or ""
    if not text:
        await update.message.reply_text("⚠️ Couldn't read that message. Try forwarding a text message.")
        return

    parsed = parse_vendor_message(text)

    if not parsed["product_name"] or parsed["lowest_price"] == 0.0:
        await update.message.reply_text(
            "⚠️ *Couldn't auto-parse this message.*\n\n"
            "Please type the product manually in this format:\n"
            "`Product Name | $price`\n\n"
            "Example: `Mix & Match Flower | $95`",
            parse_mode="Markdown"
        )
        context.user_data["awaiting_manual_product"] = True
        return

    success = await add_product_to_notion(
        parsed["product_name"],
        parsed["lowest_price"],
        parsed["strains"],
        parsed["price_tiers"]
    )

    if success:
        strain_preview = ", ".join(parsed["strains"][:3])
        if len(parsed["strains"]) > 3:
            strain_preview += f" + {len(parsed['strains']) - 3} more"
        tiers_preview = " | ".join(parsed["price_tiers"])

        price_display = tiers_preview if tiers_preview else f"${parsed['lowest_price']:.2f}"
        await update.message.reply_text(
            f"✅ *Added to menu!*\n\n"
            f"📦 {parsed['product_name']}\n"
            f"🌿 Strains: {strain_preview or 'None detected'}\n"
            f"💵 Prices: {price_display}\n\n"
            f"Forward another message to keep adding, or check Notion to review.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Failed to save to Notion. Check your connection and try again.")


async def handle_manual_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle manually typed product in 'Name | $price' format."""
    if update.effective_chat.id != ADMIN_CHAT_ID:
        return

    if not context.user_data.get("awaiting_manual_product"):
        return

    import re
    text = update.message.text.strip()

    if "|" not in text:
        await update.message.reply_text(
            "⚠️ Please use this format:\n`Product Name | $price`\n\nExample: `Mix & Match Flower | $95`",
            parse_mode="Markdown"
        )
        return

    parts = text.split("|")
    product_name = parts[0].strip()
    price_match  = re.search(r'\$?\s*(\d+(?:\.\d{1,2})?)', parts[1])
    price = float(price_match.group(1)) if price_match else None

    if not price:
        await update.message.reply_text("⚠️ Couldn't find a price. Try again: `Product Name | $price`", parse_mode="Markdown")
        return

    success = await add_product_to_notion(product_name, price, [], [])
    context.user_data["awaiting_manual_product"] = False

    if success:
        await update.message.reply_text(
            f"✅ *Added to menu!*\n\n"
            f"📦 {product_name}\n"
            f"💵 ${price:.2f}\n\n"
            f"Forward another vendor message or go to Notion to review.",
            parse_mode="Markdown"
        )
    else:
        await update.message.reply_text("⚠️ Failed to save to Notion. Try again.")


async def add_product_to_notion(name: str, price: float, strains: list, price_tiers: list) -> bool:
    """Add a new product to the HGBot Products Notion database."""
    url = "https://api.notion.com/v1/pages"
    strains_str    = ", ".join(strains) if strains else ""
    price_tiers_str = " | ".join(price_tiers) if price_tiers else f"${price:.2f}"

    payload = {
        "parent": {"database_id": NOTION_PRODUCTS_DB_ID},
        "properties": {
            "Product Name": {"title": [{"text": {"content": name}}]},
            "Price (USD)":  {"number": price},
            "Strains":      {"rich_text": [{"text": {"content": strains_str}}]},
            "Price Tiers":  {"rich_text": [{"text": {"content": price_tiers_str}}]},
            "Active":       {"checkbox": True},
        }
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=notion_headers()) as resp:
                result = await resp.json()
                return "id" in result
    except Exception as e:
        logger.error(f"Notion add product error: {e}")
        return False


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

    # Build product detail message
    msg = f"*{product['name']}*\n"
    if product.get("price_tiers"):
        msg += f"💵 *Prices:* {product['price_tiers']}\n"
    else:
        msg += f"💵 ${product['price']:.2f} each\n"
    if product.get("strains"):
        strain_list = product["strains"].split(", ")
        strains_formatted = "\n".join(f"  • {s}" for s in strain_list)
        msg += f"\n🌿 *Available Strains:*\n{strains_formatted}\n"
    msg += "\nHow many would you like?"

    image_url = product.get("image_url", "")

    if image_url:
        # Send image with caption and buttons, delete the old menu message
        await query.message.delete()
        await query.message.chat.send_photo(
            photo=image_url,
            caption=msg,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    else:
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=keyboard)

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
    app.add_handler(CommandHandler("syncmenu", syncmenu))
    app.add_handler(CallbackQueryHandler(forward_confirmed, pattern="^forward_"))
    # Handle forwarded messages and manual product entry from admin
    app.add_handler(MessageHandler(
        filters.FORWARDED & filters.Chat(ADMIN_CHAT_ID), handle_forwarded_product
    ))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND & filters.Chat(ADMIN_CHAT_ID), handle_manual_product
    ))

    print("🌿 HGBot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
