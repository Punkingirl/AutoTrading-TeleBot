import asyncio
import configparser
import logging
import re
import MetaTrader5 as mt5
from telethon import TelegramClient, events

# Load config
config = configparser.ConfigParser()
config.read('config.ini')

API_ID = int(config['Telegram']['api_id'])
API_HASH = config['Telegram']['api_hash']
PHONE = config['Telegram']['phone_number']
SOURCE_CHANNEL_ID = int(config['Telegram']['source_channel_id'])
MT5_LOGIN = int(config['MetaTrader']['login'])
MT5_PASSWORD = config['MetaTrader']['password']
MT5_SERVER = config['MetaTrader']['server']
LOT_SIZE = float(config['Settings']['lot_size'])
TP1_LOT_SIZE = float(config['Settings'].get('tp1_lot_size', LOT_SIZE))

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
log = logging.getLogger(__name__)

# Track placed signals to avoid duplicates
placed_signals = set()

# Last successfully placed signal (used for reenter)
last_signal = None

# Ticket numbers from last placed signal {tp_key: ticket}
last_tickets = {}

# Flag to cancel TP1 monitoring if needed
sl_monitor_task = None

# Message IDs that have already triggered a reenter (prevents double placement)
reenter_processed_ids = set()

# Connect to MT5
def connect_mt5():
    if not mt5.initialize():
        log.error("MT5 initialize failed")
        return False
    if not mt5.login(MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
        log.error(f"MT5 login failed: {mt5.last_error()}")
        return False
    log.info(f"✅ Connected to MT5 - Account: {MT5_LOGIN}")
    return True

# Parse signal message
def parse_signal(text):
    signal = {}
    text = text.strip()

    if re.search(r'\bbuy\b', text, re.IGNORECASE):
        signal['direction'] = 'buy'
    elif re.search(r'\bsell\b', text, re.IGNORECASE):
        signal['direction'] = 'sell'
    else:
        return None

    sym = re.search(r'\b(XAUUSD|EURUSD|GBPUSD|USDJPY|GBPJPY|XAGUSD|[A-Z]{6})\b', text)
    if sym:
        signal['symbol'] = sym.group(1)
    else:
        return None

    entry = re.search(r'Enter[:\s]+([\d.]+)', text, re.IGNORECASE)
    sl    = re.search(r'SL[:\s]+([\d.]+)', text, re.IGNORECASE)
    tp1   = re.search(r'TP1[:\s]+([\d.]+)', text, re.IGNORECASE)
    tp2   = re.search(r'TP2[:\s]+([\d.]+)', text, re.IGNORECASE)
    tp3   = re.search(r'TP3[:\s]+([\d.]+)', text, re.IGNORECASE)
    tp4   = re.search(r'TP4[:\s]+([\d.]+)', text, re.IGNORECASE)

    if entry: signal['entry'] = float(entry.group(1))
    if sl:    signal['sl']    = float(sl.group(1))
    if tp1:   signal['tp1']   = float(tp1.group(1))
    if tp2:   signal['tp2']   = float(tp2.group(1))
    if tp3:   signal['tp3']   = float(tp3.group(1))
    if tp4:   signal['tp4']   = float(tp4.group(1))

    return signal if 'sl' in signal and 'tp1' in signal else None

def get_signal_key(signal):
    """Create a unique key for a signal to detect duplicates"""
    return f"{signal['symbol']}_{signal['direction']}_{signal.get('entry')}_{signal.get('tp1')}"

def reconstruct_price(shorthand, reference_price):
    """Expand a 2-digit shorthand price (e.g. 65) to full price (e.g. 4665)
    using the current market price as reference."""
    if shorthand >= 1000:
        return float(shorthand)  # already a full price
    base = int(reference_price / 100) * 100
    candidate = base + shorthand
    # Check adjacent hundreds in case we're near a boundary
    for alt in [candidate + 100, candidate - 100]:
        if abs(reference_price - alt) < abs(reference_price - candidate):
            candidate = alt
    return float(candidate)

def parse_reenter(text):
    """Parse a reenter message. Handles:
      - 'Reenter 65 / SL 25'  → specific price and SL
      - 'Reenter'             → market price, use last signal's SL
    """
    if not re.search(r'\bre-?enter\b', text, re.IGNORECASE):
        return None
    entry_match = re.search(r're-?enter[:\s]+([\d.]+)', text, re.IGNORECASE)
    sl_match    = re.search(r'SL[:\s]+([\d.]+)', text, re.IGNORECASE)
    return {
        'entry_short': float(entry_match.group(1)) if entry_match else None,
        'sl_short':    float(sl_match.group(1))    if sl_match    else None,
    }

# Place order in MT5
MAX_SPREAD = 1.0  # max acceptable spread in price units (e.g. $1.00 for XAUUSD)

def place_order(signal, tp_value, label, lot_size=LOT_SIZE):
    symbol = signal['symbol']
    mt5.symbol_select(symbol, True)

    info = mt5.symbol_info(symbol)
    if info is None or info.trade_mode != mt5.SYMBOL_TRADE_MODE_FULL:
        log.warning(f"⚠️ Market not fully open for {symbol}, skipping order")
        return None

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        log.error(f"❌ Could not get tick data for {symbol}")
        return None

    spread = tick.ask - tick.bid
    if spread > MAX_SPREAD:
        log.warning(f"⚠️ Spread too wide ({spread:.2f}), skipping {label}")
        return None
    log.info(f"📊 Spread: {spread:.2f}")

    order_type = mt5.ORDER_TYPE_BUY if signal['direction'] == 'buy' else mt5.ORDER_TYPE_SELL
    price = tick.ask if signal['direction'] == 'buy' else tick.bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": lot_size,
        "type": order_type,
        "price": price,
        "sl": signal.get('sl', 0),
        "tp": tp_value,
        "comment": f"TG Signal {label}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_RETURN,
    }

    for filling in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN, mt5.ORDER_FILLING_FOK]:
        request["type_filling"] = filling
        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f"✅ Order placed: {signal['direction'].upper()} {symbol} {label} @ {price} TP={tp_value} (filling={filling})")
            return result
        elif result.retcode == 10030:
            log.warning(f"⚠️ Filling mode {filling} rejected, trying next...")
            continue
        else:
            log.error(f"❌ Order failed: {result.comment} (code: {result.retcode})")
            return result

    log.error(f"❌ All filling modes failed for {symbol} {label}")
    return result

def process_signal(signal):
    """Process and place orders for a signal, avoiding duplicates"""
    global last_signal, last_tickets
    key = get_signal_key(signal)
    if key in placed_signals:
        log.info(f"⏭️ Signal already placed, skipping: {key}")
        return
    log.info(f"🚦 Placing orders for: {signal}")
    success = False
    tickets = {}
    for tp_key in ['tp1', 'tp2', 'tp3', 'tp4']:
        if tp_key in signal:
            lot = TP1_LOT_SIZE if tp_key == 'tp1' else LOT_SIZE
            result = place_order(signal, signal[tp_key], tp_key.upper(), lot_size=lot)
            if result is None:
                continue
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                tickets[tp_key] = result.order
                success = True
    if success:
        placed_signals.add(key)
        last_signal = signal
        last_tickets = tickets
        log.info(f"🎫 Tickets: {last_tickets}")
    else:
        log.warning(f"⚠️ No orders succeeded for {key} — will retry on next edit")

def close_all_positions(symbol):
    """Close all open positions for the given symbol at market price"""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        log.info(f"ℹ️ No open positions to close for {symbol}")
        return
    for pos in positions:
        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            log.error(f"❌ Could not get tick to close #{pos.ticket}")
            continue
        close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
        price = tick.bid if pos.type == mt5.ORDER_TYPE_BUY else tick.ask
        request = {
            "action":   mt5.TRADE_ACTION_DEAL,
            "position": pos.ticket,
            "symbol":   symbol,
            "volume":   pos.volume,
            "type":     close_type,
            "price":    price,
            "comment":  "TG Fully Close",
            "type_time": mt5.ORDER_TIME_GTC,
        }
        for filling in [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_RETURN, mt5.ORDER_FILLING_FOK]:
            request["type_filling"] = filling
            result = mt5.order_send(request)
            if result.retcode == mt5.TRADE_RETCODE_DONE:
                log.info(f"✅ Closed position #{pos.ticket} @ {price}")
                break
            elif result.retcode == 10030:
                continue
            else:
                log.error(f"❌ Failed to close #{pos.ticket}: {result.comment} (code: {result.retcode})")
                break

def handle_fully_close(text):
    """Detect 'Fully close' and close all open positions"""
    if not re.search(r'\bfully\s+close\b', text, re.IGNORECASE):
        return False
    if not last_signal:
        log.warning("⚠️ Fully close received but no previous signal to reference")
        return True
    log.info(f"🚪 Closing all open {last_signal['symbol']} positions...")
    close_all_positions(last_signal['symbol'])
    return True

def move_sl_to_entry(symbol):
    """Move SL to entry price for all open positions on the given symbol"""
    positions = mt5.positions_get(symbol=symbol)
    if not positions:
        log.info(f"ℹ️ No open positions found for {symbol}")
        return
    for pos in positions:
        request = {
            "action":   mt5.TRADE_ACTION_SLTP,
            "position": pos.ticket,
            "symbol":   symbol,
            "sl":       pos.price_open,
            "tp":       pos.tp,
        }
        result = mt5.order_send(request)
        if result.retcode == mt5.TRADE_RETCODE_DONE:
            log.info(f"✅ SL moved to entry ({pos.price_open}) for ticket #{pos.ticket}")
        elif result.retcode == 10025:  # No changes — already set
            log.info(f"ℹ️ SL already at entry for ticket #{pos.ticket}")
        else:
            log.error(f"❌ Failed to move SL for #{pos.ticket}: {result.comment} (code: {result.retcode})")

async def monitor_tp1_then_move_sl(symbol, tp1_ticket):
    """Watch for TP1 to close, then move SL to entry on remaining positions"""
    global sl_monitor_task
    log.info(f"👁️ Monitoring TP1 ticket #{tp1_ticket} — will move SL to entry when hit...")
    while True:
        await asyncio.sleep(5)
        positions = mt5.positions_get(ticket=tp1_ticket)
        if not positions:
            log.info(f"🎯 TP1 hit! Moving SL to entry for remaining {symbol} positions...")
            move_sl_to_entry(symbol)
            sl_monitor_task = None
            return

def handle_sl_to_entry(text):
    """Detect SL entry messages:
    - 'SL entry TP1' → arm monitor, move SL to entry once TP1 closes
    - 'SL entry'     → immediately move SL to entry for all open positions
    """
    global sl_monitor_task
    if not re.search(r'\bSL\s+entry\b', text, re.IGNORECASE):
        return False
    if not last_signal:
        log.warning("⚠️ SL entry received but no previous signal to reference")
        return True

    # 'SL entry TP1' — wait for TP1 to close first
    if re.search(r'\bTP1\b', text, re.IGNORECASE):
        tp1_ticket = last_tickets.get('tp1')
        if not tp1_ticket:
            log.warning("⚠️ No TP1 ticket found — cannot monitor")
            return True
        if sl_monitor_task and not sl_monitor_task.done():
            sl_monitor_task.cancel()
        sl_monitor_task = asyncio.create_task(
            monitor_tp1_then_move_sl(last_signal['symbol'], tp1_ticket)
        )
        log.info(f"🔒 SL-to-entry armed — waiting for TP1 (ticket #{tp1_ticket}) to close")
    else:
        # 'SL entry' alone — move SL to entry immediately
        log.info(f"🔒 Moving SL to entry immediately for all open {last_signal['symbol']} positions...")
        move_sl_to_entry(last_signal['symbol'])

    return True

def handle_reenter(text, msg_id=None):
    """Try to process a reenter message using the last placed signal's TPs"""
    reenter = parse_reenter(text)
    if not reenter:
        return False
    if msg_id is not None:
        if msg_id in reenter_processed_ids:
            log.info(f"⏭️ Reenter already processed for message {msg_id}, skipping")
            return True
        reenter_processed_ids.add(msg_id)
    if not last_signal:
        log.warning("⚠️ Reenter received but no previous signal to reference")
        return True
    tick = mt5.symbol_info_tick(last_signal['symbol'])
    if not tick:
        log.error(f"❌ Could not get tick for reenter on {last_signal['symbol']}")
        return True
    ref = tick.ask if last_signal['direction'] == 'buy' else tick.bid

    # Use provided shorthand price or fall back to current market price
    entry = reconstruct_price(reenter['entry_short'], ref) if reenter['entry_short'] else ref
    # Use provided SL or fall back to last signal's SL
    sl    = reconstruct_price(reenter['sl_short'], ref) if reenter['sl_short'] else last_signal.get('sl', 0)

    signal = {**last_signal, 'entry': entry, 'sl': sl}
    log.info(f"🔄 Reenter signal: {last_signal['direction'].upper()} {last_signal['symbol']} @ {entry} SL={sl}")
    process_signal(signal)
    return True

# Main bot
async def main():
    if not connect_mt5():
        return

    client = TelegramClient('session', API_ID, API_HASH)
    await client.start(phone=PHONE)
    log.info("✅ Connected to Telegram - Listening for signals...")

    @client.on(events.NewMessage(chats=SOURCE_CHANNEL_ID))
    async def handler(event):
        msg_id = event.message.id
        log.info(f"📩 New message received (id={msg_id}), waiting 5s for edits...")
        await asyncio.sleep(5)
        # Re-fetch the latest version of the message in case it was edited
        msg = await client.get_messages(SOURCE_CHANNEL_ID, ids=msg_id)
        text = msg.message
        log.info(f"📩 Processing message: {text[:80]}")
        signal = parse_signal(text)
        if signal:
            process_signal(signal)
        elif handle_fully_close(text):
            pass
        elif handle_sl_to_entry(text):
            pass
        elif not handle_reenter(text, msg_id=msg_id):
            log.info("ℹ️ No valid signal found in message")

    @client.on(events.MessageEdited(chats=SOURCE_CHANNEL_ID))
    async def edit_handler(event):
        msg_id = event.message.id
        text = event.message.message
        log.info(f"✏️ Edited message: {text[:80]}")
        signal = parse_signal(text)
        if signal:
            process_signal(signal)
        elif handle_fully_close(text):
            pass
        elif handle_sl_to_entry(text):
            pass
        elif not handle_reenter(text, msg_id=msg_id):
            log.info("ℹ️ No valid signal found in edited message")

    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())