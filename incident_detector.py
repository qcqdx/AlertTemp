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

previous_messages = {}  # словарь для хранения ID предыдущих сообщений


def get_previous_incident_type(current_id, tab_id, sensor_name):
    while current_id > 0:
        current_id -= 1
        cur_incidents.execute("SELECT event, tab_id, sensor FROM incidents WHERE id=?", (current_id,))
        row = cur_incidents.fetchone()

        if not row:
            continue

        event, prev_tab_id, prev_sensor = row

        # Проверяем соответствие tab_id и sensor_name
        if prev_tab_id == tab_id and prev_sensor == sensor_name and event in ['Перегрев', 'Переохлаждение']:
            return event

    return None  # Если не найдено соответствующего инцидента


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
        'Возврат в норму': '🍀'
    }.get(incident[2], '🚨')

    datetime_str = incident[1]
    datetime_obj = datetime.fromisoformat(datetime_str.replace("Z", "+00:00")) + timedelta(hours=3)
    datetime_corrected_str = datetime_obj.strftime('%d.%m.%Y в %H:%M:%S')

    if incident[2] == 'Возврат в норму' and prev_incident_type:
        hours, minutes, rest = incident[7].split(':')
        seconds, milliseconds = map(int, rest.split('.'))
        total_seconds = int(hours) * 3600 + int(minutes) * 60 + seconds

        if milliseconds >= 500:
            total_seconds += 1

        downtime = str(timedelta(seconds=total_seconds))

        message = (f"<b>{tab_name}, {incident[4]},\n{emoji} {incident[2]},</b> со значением: <b>{incident[5]} ℃.</b>"
                   f"\nВ состоянии <b>{prev_incident_type}</b> находился <b>{downtime}</b>"
                   f"\n<i>Зафиксировано:</i> \n<b>{datetime_corrected_str}</b>\n")

    else:
        message = (f"<b>{tab_name}, {incident[4]},\n{emoji} {incident[2]},</b> со значением: <b>{incident[5]} ℃.</b>"
                   f"\n<i>Зафиксировано:</i> \n<b>{datetime_corrected_str}</b>")

    return message


async def send_incident(incident):
    message_to_send = format_message(incident)
    key = (convert_tab_id(incident[3]), incident[4])

    for bot in bots:
        for chat_id in chat_ids:
            if incident[2] == 'Возврат в норму' and key in previous_messages:
                # Достаем последний message_id из списка для данного ключа
                reply_to_id = previous_messages[key].pop()

                # Отправляем сообщение как ответ на предыдущее
                await bot.send_message(chat_id[0], message_to_send, parse_mode='HTML', reply_to_message_id=reply_to_id)

                # Если после извлечения message_id список для данного ключа стал пустым, удаляем ключ
                if not previous_messages[key]:
                    del previous_messages[key]
            else:
                sent_message = await bot.send_message(chat_id[0], message_to_send, parse_mode='HTML')

                # Сохраняем message_id только для определенных типов инцидентов
                if incident[2] in ['Перегрев', 'Переохлаждение']:
                    if key not in previous_messages:
                        previous_messages[key] = []
                    previous_messages[key].append(sent_message.message_id)


# Get the id of the latest incident
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
            for incident in new_incidents:
                loop.run_until_complete(send_incident(incident))
                last_id = incident[0]
        time.sleep(10)
except KeyboardInterrupt:
    for bot in bots:
        loop.run_until_complete(bot.close())
