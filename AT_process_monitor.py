import os
import subprocess
import threading
import time
import signal
import sqlite3
import logging

# Глобальная переменная, указывающая, должен ли скрипт продолжать работать.
should_continue = True

# Путь к файлу журнала
log_file = os.path.join('instance', 'process_monitor.log')

# Настройка логирования
logging.basicConfig(filename=log_file,
                    level=logging.INFO,
                    format='[%(asctime)s] %(levelname).1s %(message)s',
                    datefmt='%Y.%m.%d %H:%M:%S')


def monitor_process(command):
    process = None
    while should_continue:
        if process is None or (process.poll() is not None):
            logging.info(f"Запускается процесс: {command}")
            process = subprocess.Popen(command, shell=True)
        time.sleep(1)

    if process is not None:
        logging.info(f"Процесс остановлен: {command}")
        process.terminate()


def signal_handler(signum, frame):
    global should_continue
    should_continue = False


def check_database_and_table(db_path, tbl_name):
    if os.path.exists(db_path):
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = cursor.fetchall()
            if tbl_name in [table[0] for table in tables]:
                return True
        except Exception as err:
            logging.error(f"Ошибка при проверке базы данных и таблицы: {err}")
            return False
    return False


# Обработчики для сигналов SIGTERM и SIGINT.
signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

database_path = 'instance/sensors_data.db'
table_name = 'temp_records'

commands = ["python3 data_collector.py", "python3 data_sorter.py", "python3 incident_detector.py",
            "python3 viz_app.py", "python3 event_notification.py"]

# Создание каталога для файла журнала, если он еще не существует
os.makedirs(os.path.dirname(log_file), exist_ok=True)

try:
    threads = []
    for command in commands:
        thread = threading.Thread(target=monitor_process, args=(command,))
        thread.start()
        threads.append(thread)

        while not check_database_and_table(database_path, table_name):
            time.sleep(1)

    for thread in threads:
        thread.join()

except Exception as e:
    logging.error(f"Возникла ошибка: {e}")
