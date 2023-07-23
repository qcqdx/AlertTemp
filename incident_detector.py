import sqlite3
from datetime import datetime
import signal
import sys
import time


def create_incidents_db(cursor):
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS incidents ("
        "id INTEGER PRIMARY KEY,"
        "datetime TEXT NOT NULL,"
        "event TEXT NOT NULL,"
        "tab_id INTEGER NOT NULL,"
        "sensor TEXT NOT NULL,"
        "value REAL NOT NULL,"
        "peak REAL,"
        "duration TEXT)"
    )


def create_states_db(cursor):
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS states ("
        "sensor TEXT PRIMARY KEY,"
        "state TEXT NOT NULL)"
    )


def create_last_processed_timestamp_db(cursor):
    cursor.execute(
        "CREATE TABLE IF NOT EXISTS last_processed ("
        "id INTEGER PRIMARY KEY,"
        "timestamp TEXT NOT NULL)"
    )


def load_last_processed_timestamp(cursor):
    cursor.execute("SELECT timestamp FROM last_processed ORDER BY id DESC LIMIT 1")
    result = cursor.fetchone()
    return result[0] if result else None


def get_peak_and_start_time(sensor, start_time, end_time, cursor):
    cursor.execute(
        "SELECT MAX(value), MIN(timestamp) FROM temp_records WHERE topic = ? AND timestamp BETWEEN ? AND ?",
        (sensor, start_time, end_time)
    )
    res = cursor.fetchone()
    return res if res else (None, None)


def update_peak_values_for_return_to_normal(cursor, sensors_cursor):
    # print("Starting to update peak values...")
    cursor.execute("SELECT id, datetime, sensor FROM incidents WHERE event = 'Возврат в норму' AND peak IS NULL")
    records_to_update = cursor.fetchall()

    for record in records_to_update:
        record_id, end_time, sensor = record
        cursor.execute("SELECT datetime FROM incidents WHERE sensor = ? AND event IN ('Перегрев', 'Переохлаждение') "
                       "AND datetime < ? ORDER BY datetime DESC LIMIT 1", (sensor, end_time))
        start_time_data = cursor.fetchone()

        if not start_time_data:
            # print(f"No start time found for record {record_id}.")
            continue

        start_time = start_time_data[0]
        sensors_cursor.execute("SELECT MAX(value) FROM temp_records WHERE topic LIKE ? AND timestamp BETWEEN ? AND ?",
                               ('%' + sensor + '%', start_time, end_time))
        peak_value_data = sensors_cursor.fetchone()

        if not peak_value_data:
            # print(f"No peak value data found for record {record_id} between {start_time} and {end_time}.")
            continue

        peak_value = peak_value_data[0]
        # print(f"Updating record {record_id} with peak value {peak_value}.")
        cursor.execute("UPDATE incidents SET peak = ? WHERE id = ?", (peak_value, record_id))


def delete_old_incidents(cursor):
    # This function can be expanded based on the logic to delete old incidents
    pass


def get_tab_and_range(cursor, sensor_name):
    # Получение tab_id, overheat и overcool из таблиц ranges и tab_settings
    cursor.execute("""
        SELECT 
            ranges.tab_id, ranges.overheat, ranges.overcool, 
            CASE 
                WHEN tab_settings.table1 = ? THEN tab_settings.table1_alias
                WHEN tab_settings.table2 = ? THEN tab_settings.table2_alias
                WHEN tab_settings.table3 = ? THEN tab_settings.table3_alias
                ELSE NULL
            END as sensor_alias
        FROM ranges
        JOIN tab_settings ON ranges.tab_id = tab_settings.tab_id
        WHERE tab_settings.table1 = ? OR tab_settings.table2 = ? OR tab_settings.table3 = ?
        """, (sensor_name, sensor_name, sensor_name, sensor_name, sensor_name, sensor_name))

    result = cursor.fetchone()
    return result if result else (None, None, None, None)


def get_peak_value(sensor, start_time, end_time, cursor):
    """Функция для получения пикового значения датчика между start_time и end_time."""
    cursor.execute(
        "SELECT MAX(value) FROM temp_records WHERE topic = ? AND timestamp BETWEEN ? AND ?",
        (sensor, start_time, end_time)
    )
    res = cursor.fetchone()
    return res[0] if res else None


def get_last_state(cursor, sensor):
    cursor.execute("SELECT state FROM states WHERE sensor = ?", (sensor,))
    result = cursor.fetchone()
    return result[0] if result else None


def save_last_processed_timestamp(cursor, timestamp):
    cursor.execute("INSERT INTO last_processed (timestamp) VALUES (?)", (timestamp,))


def main():
    def signal_handler(sig, frame):
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)

    incidents_conn = sqlite3.connect('instance/incidents.db')
    incidents_cur = incidents_conn.cursor()
    settings_conn = sqlite3.connect('instance/user_settings.db')
    settings_cur = settings_conn.cursor()
    sensors_conn = sqlite3.connect('instance/sensors_data.db')
    sensors_cur = sensors_conn.cursor()

    try:
        create_incidents_db(incidents_cur)
        create_states_db(incidents_cur)
        create_last_processed_timestamp_db(incidents_cur)
        last_processed_timestamp = load_last_processed_timestamp(incidents_cur)

        while True:
            if last_processed_timestamp is None:
                sensors_cur.execute("SELECT timestamp, topic, value FROM temp_records ORDER BY timestamp")
            else:
                sensors_cur.execute("SELECT timestamp, topic, value FROM temp_records "
                                    "WHERE timestamp > ? ORDER BY timestamp", (last_processed_timestamp,))

            delete_old_incidents(incidents_cur)

            for row in sensors_cur:
                timestamp, sensor, value = row
                last_processed_timestamp = timestamp
                peak_value = None  # Инициализация перед использованием
                duration = None  # Инициализация перед использованием
                sensor_name = sensor.replace('/', '_')
                tab_id, overheat, overcool, sensor_alias = get_tab_and_range(settings_cur, sensor_name)

                if tab_id is None or overheat is None or overcool is None:
                    continue

                sensor = sensor_alias if sensor_alias else sensor_name
                last_state = get_last_state(incidents_cur, sensor)

                if value > overheat:
                    new_state = "Перегрев"
                elif value < overcool:
                    new_state = "Переохлаждение"
                else:
                    if last_state in ["Перегрев", "Переохлаждение"]:
                        # Получение timestamp начала инцидента
                        incidents_cur.execute("SELECT datetime FROM incidents WHERE sensor = ? AND event = ? "
                                              "ORDER BY datetime DESC LIMIT 1", (sensor, last_state))
                        incident_start_timestamp = incidents_cur.fetchone()
                        if incident_start_timestamp:
                            incident_start_timestamp = incident_start_timestamp[0]
                            peak_value = get_peak_value(sensor_name, incident_start_timestamp, timestamp, sensors_cur)
                            duration = (
                                    datetime.fromisoformat(timestamp) -
                                    datetime.fromisoformat(incident_start_timestamp)
                            )
                            duration = str(duration)
                        else:
                            peak_value, duration = None, None
                    else:
                        peak_value, duration = None, None
                    new_state = "Возврат в норму"

                incidents_cur.execute("INSERT OR REPLACE INTO states (sensor, state) "
                                      "VALUES (?, ?)", (sensor, new_state))

                if new_state != last_state:
                    incidents_cur.execute(
                        "INSERT INTO incidents (datetime, event, tab_id, sensor, value, peak, duration) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (timestamp, new_state, tab_id, sensor, value, peak_value, duration))

            save_last_processed_timestamp(incidents_cur, last_processed_timestamp)
            update_peak_values_for_return_to_normal(incidents_cur, sensors_cur)
            incidents_conn.commit()

            time.sleep(1)  # задержка в 1 секунду

    except Exception as e:
        print(f"An error occurred: {e}")
        incidents_conn.rollback()

    finally:
        incidents_conn.close()
        sensors_conn.close()
        settings_conn.close()


if __name__ == '__main__':
    main()
