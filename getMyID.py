from telethon.sync import TelegramClient

api_id = 32601262
api_hash = 'df7961f695296dc5a2591fc6f049294d'

with TelegramClient('session', api_id, api_hash) as client:
    me = client.get_me()
    print(f"Username: {me.username}")
    print(f"User ID: {me.id}")