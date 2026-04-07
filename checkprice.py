import MetaTrader5 as mt5
import configparser

config = configparser.ConfigParser()
config.read('config.ini')

mt5.initialize()
mt5.login(int(config['MetaTrader']['login']), 
          password=config['MetaTrader']['password'],
          server=config['MetaTrader']['server'])

tick = mt5.symbol_info_tick('XAUUSD')
info = mt5.symbol_info('XAUUSD')
print(f"Current XAUUSD price: {tick.ask}")
print(f"Stop level: {info.trade_stops_level}")
print(f"Point size: {info.point}")
print(f"Min stop distance: {info.trade_stops_level * info.point}")
print(f"Filling modes: {info.filling_mode}")
print(f"Order modes: {info.order_mode}")
print(f"Spread: {mt5.symbol_info_tick('XAUUSD').ask - mt5.symbol_info_tick('XAUUSD').bid}")