from telethon.sync import TelegramClient

api_id = 32601262
api_hash = 'df7961f695296dc5a2591fc6f049294d'
phone = '+64273437614' 

with TelegramClient('session', api_id, api_hash) as client:
    for dialog in client.iter_dialogs():
        if 'fred' in dialog.name.lower():
            print(f"Name: {dialog.name}")
            print(f"ID: {dialog.id}")