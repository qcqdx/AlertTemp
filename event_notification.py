import sqlite3
import asyncio
from datetime import datetime, timedelta
from aiogram import Bot
import time
import logging

logging.basicConfig(filename='instance/evnot_log', level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

conn_incidents = sqlite3.connect('instance/incidents.db')
cur_incidents = conn_incidents.cursor()
conn_settings = sqlite3.connect('instance/user_settings.db')
cur_settings = conn_settings.cursor()

logging.info('Connected to databases.')

cur_settings.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='bots'")
if not cur_settings.fetchone():
    cur_settings.execute("""
    CREATE TABLE bots (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        api_key TEXT NOT NULL
    )
    """)
    conn_settings.commit()
    logging.info("Created 'bots' table.")

cur_settings.execute("SELECT api_key FROM bots")
bot_api_keys = cur_settings.fetchall()
logging.info(f"Fetched {len(bot_api_keys)} bot API keys.")

cur_settings.execute("SELECT userid FROM users")
chat_ids = cur_settings.fetchall()
logging.info(f"Fetched {len(chat_ids)} chat IDs.")

bots = [Bot(api_key[0]) for api_key in bot_api_keys]
logging.info('Initialized Telegram bots.')

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

previous_messages = {}
sent_incident_ids = set()


def get_previous_incident_type(current_id, tab_id, sensor_name):
    while current_id > 0:
        current_id -= 1
        cur_incidents.execute("SELECT event, tab_id, sensor FROM incidents WHERE id=?", (current_id,))
        row = cur_incidents.fetchone()

        if not row:
            continue

        event, prev_tab_id, prev_sensor = row

        if prev_tab_id == tab_id and prev_sensor == sensor_name and event in ['Перегрев', 'Переохлаждение']:
            return event

    return None


def convert_tab_id(tab_id):
    cur_settings.execute("SELECT tab_name FROM tabs WHERE id = ?", (tab_id,))
    tab_name = cur_settings.fetchone()[0]
    return tab_name


def format_message(incident):
    tab_name = convert_tab_id(incident[3])
    prev_incident_type = get_previous_incident_type(incident[0], incident[3], incident[4]) if incident[
                                                                                                  2] == 'Возврат в норму' else ""

    emoji = {
        'Перегрев': '🔥',
        'Переохлаждение': '❄️',
        'Возврат в норму': '🍀',
        'Критический перегрев': '💥',
        'Критическое переохлаждение': '🌬️'
    }.get(incident[2], '🚨')

    datetime_str = incident[1]
    datetime_obj = datetime.fromisoformat(datetime_str.replace("Z", "+00:00")) + timedelta(hours=3)
    datetime_corrected_str = datetime_obj.strftime('%d.%m.%Y в %H:%M:%S')

    if incident[2] == 'Возврат в норму' and prev_incident_type:
        if 'day' in incident[7]:  # Если присутствует день
            days_str, time_str = incident[7].split(' day, ')
            days = int(days_str)
            hours, minutes, rest = time_str.split(':')
            logging.info(f"Downtime exceeded a day. Total days: {days}.")
        else:
            days = 0
            hours, minutes, rest = incident[7].split(':')

        seconds, milliseconds = map(int, rest.split('.'))
        total_seconds = days * 86400 + int(hours) * 3600 + int(minutes) * 60 + seconds

        if milliseconds >= 500:
            total_seconds += 1

        downtime = str(timedelta(seconds=total_seconds))
        logging.info(f"Total downtime in seconds: {total_seconds}.")

        message = (f"<b>{tab_name}, {incident[4]},\n{emoji} {incident[2]},</b> со значением: <b>{incident[5]} ℃.</b>"
                   f"\nВ состоянии <b>{prev_incident_type}</b> находился <b>{downtime}</b>"
                   f"\n<i>Зафиксировано:</i> \n<b>{datetime_corrected_str}</b>\n")


    else:
        message = (f"<b>{tab_name}, {incident[4]},\n{emoji} {incident[2]},</b> со значением: <b>{incident[5]} ℃.</b>"
                   f"\n<i>Зафиксировано:</i> \n<b>{datetime_corrected_str}</b>")

    return message


async def send_incident(incident):
    incident_id = incident[0]

    if incident_id in sent_incident_ids:
        return

    message_to_send = format_message(incident)
    key_base = (convert_tab_id(incident[3]), incident[4])

    for bot in bots:
        for chat_id in chat_ids:
            key = (*key_base, chat_id[0])

            try:
                if incident[2] == 'Возврат в норму' and key in previous_messages:
                    reply_to_id = previous_messages[key].pop()
                    sent_message = await bot.send_message(chat_id[0], message_to_send, parse_mode='HTML',
                                           reply_to_message_id=reply_to_id)
                    logging.info(
                        f"Sent 'Возврат в норму' message to chat_id {chat_id[0]} using bot {bot._token.split(':')[0]} with reply_to_id {reply_to_id}. Message: {message_to_send}")
                    logging.info(f"Message ID for 'Возврат в норму': {sent_message.message_id}")
                    if not previous_messages[key]:
                        del previous_messages[key]
                else:
                    sent_message = await bot.send_message(chat_id[0], message_to_send, parse_mode='HTML')
                    logging.info(
                        f"Sent '{incident[2]}' message to chat_id {chat_id[0]} using bot {bot._token.split(':')[0]}. Message: {message_to_send}")
                    logging.info(f"Message ID for '{incident[2]}': {sent_message.message_id}")
                    if incident[2] in ['Перегрев', 'Переохлаждение']:
                        if key not in previous_messages:
                            previous_messages[key] = []
                        previous_messages[key].append(sent_message.message_id)
            except Exception as e:
                logging.error(
                    f"Error sending message to chat_id {chat_id[0]} using bot {bot._token.split(':')[0]}: {e}")
                if "Replied message not found" in str(e):
                    logging.warning(f"Retrying to send message without reply_to_message_id due to error: {e}")
                    try:
                        sent_message = await bot.send_message(chat_id[0], message_to_send, parse_mode='HTML')
                        logging.info(
                            f"Successfully resent message to chat_id {chat_id[0]} without reply_to_message_id.")
                        logging.info(f"Resent message ID: {sent_message.message_id}")
                    except Exception as e2:
                        logging.error(f"Failed to resend message to chat_id {chat_id[0]}: {e2}")

    sent_incident_ids.add(incident_id)


cur_incidents.execute("SELECT * FROM incidents ORDER BY id DESC LIMIT 1")
last_incident = cur_incidents.fetchone()

if last_incident is not None:
    loop.run_until_complete(send_incident(last_incident))
    last_id = last_incident[0]
else:
    last_id = 0

try:
    while True:
        cur_incidents.execute("SELECT * FROM incidents WHERE id > ?", (last_id,))
        new_incidents = cur_incidents.fetchall()

        if new_incidents:
            logging.info(f"Found {len(new_incidents)} new incidents in the database.")
            for incident in new_incidents:
                loop.run_until_complete(send_incident(incident))
                last_id = incident[0]
        time.sleep(10)
except KeyboardInterrupt:
    logging.info("KeyboardInterrupt received. Exiting...")
finally:
    for bot in bots:
        loop.run_until_complete(bot.session.close())
        logging.info(f"Closed session for bot {bot._token.split(':')[0]}.")

for bot in bots:
    loop.run_until_complete(bot.close())
    logging.info(f"Closed bot {bot._token.split(':')[0]}.")

logging.info("Program finished.")
