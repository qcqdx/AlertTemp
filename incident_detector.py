import time
import sqlite3
from datetime import datetime, timedelta


def create_incidents_db(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS incidents (
            id INTEGER PRIMARY KEY,
            datetime TEXT NOT NULL,
            event TEXT NOT NULL,
            tab_id INTEGER NOT NULL,
            sensor TEXT NOT NULL,
            value REAL NOT NULL
        )
    """)


def create_states_db(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS states (
            sensor TEXT PRIMARY KEY,
            state TEXT NOT NULL
        )
    """)


def get_last_state(cur, sensor):
    cur.execute("SELECT state FROM states WHERE sensor = ?", (sensor,))
    res = cur.fetchone()
    return res[0] if res else "Норма"


def get_tab_and_range(cur, sensor):
    cur.execute("SELECT tab_settings.tab_id, overheat, overcool, "
                "CASE WHEN table1 = ? THEN table1_alias "
                "WHEN table2 = ? THEN table2_alias "
                "WHEN table3 = ? THEN table3_alias END "
                "FROM tab_settings JOIN ranges "
                "ON tab_settings.tab_id = ranges.tab_id WHERE table1 = ? OR table2 = ? OR table3 = ?",
                (sensor, sensor, sensor, sensor, sensor, sensor))
    res = cur.fetchone()
    return res if res else (None, None, None, None)


def delete_old_incidents(cur):
    six_months_ago = datetime.now() - timedelta(days=180)  # Примерное значение для полугода
    cur.execute("DELETE FROM incidents WHERE datetime < ?", (six_months_ago.isoformat(),))


def main():
    last_processed_timestamp = None
    incidents_conn = sqlite3.connect('instance/incidents.db')
    incidents_cur = incidents_conn.cursor()
    settings_conn = sqlite3.connect('instance/user_settings.db')
    settings_cur = settings_conn.cursor()
    sensors_conn = sqlite3.connect('instance/sensors_data.db')
    sensors_cur = sensors_conn.cursor()

    try:
        create_incidents_db(incidents_cur)
        create_states_db(incidents_cur)  # Creating the states table

        # Main loop
        while True:
            if last_processed_timestamp is None:
                sensors_cur.execute("SELECT timestamp, topic, value FROM temp_records ORDER BY timestamp")
            else:
                sensors_cur.execute("SELECT timestamp, topic, value FROM temp_records "
                                    "WHERE timestamp > ? ORDER BY timestamp",
                                    (last_processed_timestamp,))

            delete_old_incidents(incidents_cur)

            for row in sensors_cur:
                timestamp, sensor, value = row
                last_processed_timestamp = timestamp
                sensor_name = sensor.replace('/', '_')  # Замена '/' на '_' в sensor
                tab_id, overheat, overcool, sensor_alias = get_tab_and_range(settings_cur, sensor_name)

                # Если не удалось получить значения overheat и overcool, пропускаем текущую итерацию
                if tab_id is None or overheat is None or overcool is None:
                    continue

                # Используйте псевдоним датчика, если он доступен, иначе используйте его имя
                sensor = sensor_alias if sensor_alias else sensor_name

                # Получить последнее состояние этого датчика, если оно существует, иначе инициализировать как "Норма"
                last_state = get_last_state(incidents_cur, sensor)

                if value > overheat:
                    new_state = "Перегрев"
                elif value < overcool:
                    new_state = "Переохлаждение"
                else:
                    new_state = "Возврат в норму"

                # Обновляем состояние в базе данных в любом случае
                incidents_cur.execute(
                    "INSERT OR REPLACE INTO states (sensor, state) VALUES (?, ?)",
                    (sensor, new_state))

                # Вставляем запись об инциденте только если состояние изменилось
                if new_state != last_state:
                    incidents_cur.execute(
                        "INSERT INTO incidents (datetime, event, tab_id, sensor, value) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (timestamp, new_state, tab_id, sensor, value))

            incidents_conn.commit()

            # print("Следующая проверка через минуту.")
            time.sleep(60)
    except Exception as e:
        print(f"An error occurred: {e}")
    finally:
        incidents_conn.close()
        sensors_conn.close()
        settings_conn.close()


if __name__ == "__main__":
    main()
