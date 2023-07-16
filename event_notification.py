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

# Fetch the bot API key from the database
cur_settings.execute("SELECT api_key FROM bots LIMIT 1")  # Assuming there's only one bot
bot_api_key = cur_settings.fetchone()[0]

# Fetch the user chat_id from the database
cur_settings.execute("SELECT userid FROM users LIMIT 1")  # Assuming there's only one user
chat_id = cur_settings.fetchone()[0]

# Telegram bot setup
bot = Bot(bot_api_key)


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

    # Send the incident to the Telegram chat
    await bot.send_message(chat_id, message_to_send, parse_mode='HTML')


# Get the id of the latest incident
cur_incidents.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT 1")
last_incident = cur_incidents.fetchone()

# Send the latest incident to the Telegram chat
asyncio.run(send_incident(last_incident))

# Save the id of the latest incident
last_id = last_incident[0]

# Monitor the database for new incidents
while True:
    # Get the latest incident
    cur_incidents.execute("SELECT * FROM incidents WHERE id > ?", (last_id,))
    new_incidents = cur_incidents.fetchall()

    # If there are any new incidents
    if new_incidents:
        # Send each new incident to the Telegram chat
        for incident in new_incidents:
            asyncio.run(send_incident(incident))

        # Update the last_id
        last_id = new_incidents[-1][0]

    # Wait for a while before checking again
    time.sleep(10)
