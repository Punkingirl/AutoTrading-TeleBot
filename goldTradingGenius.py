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

# Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(message)s')
log = logging.getLogger(__name__)

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

    return signal

# Place order in MT5
def place_order(signal, tp_value, label):
    symbol = signal['symbol']
    mt5.symbol_select(symbol, True)

    order_type = mt5.ORDER_TYPE_BUY if signal['direction'] == 'buy' else mt5.ORDER_TYPE_SELL
    tick = mt5.symbol_info_tick(symbol)
    price = tick.ask if signal['direction'] == 'buy' else tick.bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": LOT_SIZE,
        "type": order_type,
        "price": price,
        "sl": signal.get('sl', 0),
        "tp": tp_value,
        "comment": f"TG Signal {label}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result.retcode == mt5.TRADE_RETCODE_DONE:
        log.info(f"✅ Order placed: {signal['direction'].upper()} {symbol} {label} @ {price} TP={tp_value}")
    else:
        log.error(f"❌ Order failed: {result.comment}")
    return result

# Main bot
async def main():
    if not connect_mt5():
        return

    client = TelegramClient('session', API_ID, API_HASH)
    await client.start(phone=PHONE)
    log.info("✅ Connected to Telegram - Listening for signals...")

    @client.on(events.NewMessage(chats=SOURCE_CHANNEL_ID))
    async def handler(event):
        text = event.message.message
        log.info(f"📩 New message: {text[:80]}")
        signal = parse_signal(text)
        if signal:
            log.info(f"🚦 Signal detected: {signal}")
            for tp_key in ['tp1', 'tp2', 'tp3', 'tp4']:
                if tp_key in signal:
                    place_order(signal, signal[tp_key], tp_key.upper())
        else:
            log.info("ℹ️ No valid signal found in message")

    @client.on(events.MessageEdited(chats=SOURCE_CHANNEL_ID))
    async def edit_handler(event):
        text = event.message.message
        log.info(f"✏️ Edited message: {text[:80]}")
        signal = parse_signal(text)
        if signal:
            log.info(f"🔄 Updated signal: {signal}")

    await client.run_until_disconnected()

if __name__ == '__main__':
    asyncio.run(main())