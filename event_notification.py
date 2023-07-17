import sqlite3
import asyncio
from datetime import datetime, timedelta
from aiogram import Bot
import time

# Connect to the SQLite database
conn_incidents = sqlite3.connect('instance/incidents.db')
cur_incidents = conn_incidents.cursor()

# Connect to the user_settings database
conn_settings = sqlite3.connect('instance/user_settings.db')
cur_settings = conn_settings.cursor()

# Fetch all bot API keys from the database
cur_settings.execute("SELECT api_key FROM bots")
bot_api_keys = cur_settings.fetchall()

# Fetch all user chat_ids from the database
cur_settings.execute("SELECT userid FROM users")
chat_ids = cur_settings.fetchall()

# Telegram bot setup
bots = [Bot(api_key[0]) for api_key in bot_api_keys]

# Create a new event loop
loop = asyncio.new_event_loop()

# Set the new event loop as the current event loop
asyncio.set_event_loop(loop)


# Function to convert tab_id to tab_name
def convert_tab_id(tab_id):
    cur_settings.execute("SELECT tab_name FROM tabs WHERE id = ?", (tab_id,))
    tab_name = cur_settings.fetchone()[0]
    return tab_name


# Define function to format message
def format_message(incident):
    # Convert the tab_id to tab_name
    tab_name = convert_tab_id(incident[3])

    # Get emoji for incident type
    if incident[2] == 'Перегрев':
        emoji = '🔥'
    elif incident[2] == 'Переохлаждение':
        emoji = '❄️'
    else:  # if incident[2] == 'Норма'
        emoji = '🍀'

    # Format date and time
    datetime_str = incident[1]
    datetime_obj = datetime.fromisoformat(datetime_str.replace("Z", "+00:00")) + timedelta(hours=3)
    datetime_corrected_str = datetime_obj.strftime('%d.%m.%Y в %H:%M:%S')

    # Format message with HTML tags for bold and italic and newlines for better readability
    message = f"{emoji} <b>{tab_name}, {incident[4]},\n{incident[2]},</b> со значением: <b>{incident[5]} ℃.</b>\n<i>Зафиксировано:</i> \n<b>{datetime_corrected_str}</b>"

    return message


async def send_incident(incident):
    # Format the incident into a message
    message_to_send = format_message(incident)

    # Send the incident to all Telegram chats from all bots
    for bot in bots:
        for chat_id in chat_ids:
            await bot.send_message(chat_id[0], message_to_send, parse_mode='HTML')

# Get the id of the latest incident
cur_incidents.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT 1")
last_incident = cur_incidents.fetchone()

# Check if there are any incidents in the table
if last_incident is not None:
    # Send the latest incident to the Telegram chat
    loop.run_until_complete(send_incident(last_incident))

    # Save the id of the latest incident
    last_id = last_incident[0]
else:
    # If there are no incidents in the table, set last_id to 0
    last_id = 0

try:
    # Monitor the database for new incidents
    while True:
        # Get the latest incident
        cur_incidents.execute("SELECT * FROM incidents WHERE id > ?", (last_id,))
        new_incidents = cur_incidents.fetchall()

        # If there are any new incidents
        if new_incidents:
            # Send each new incident to the Telegram chat
            for incident in new_incidents:
                loop.run_until_complete(send_incident(incident))

                # Update the last_id
                last_id = incident[0]

        # Wait for a while before checking again
        time.sleep(10)
except KeyboardInterrupt:
    # When the program is interrupted, close all bots
    for bot in bots:
        loop.run_until_complete(bot.close())
