import os
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from sqlite3 import Connection as SQLite3Connection
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine

lock = threading.Lock()

log_file = 'instance/data_sorter.log'
os.makedirs(os.path.dirname(log_file), exist_ok=True)
logging.basicConfig(filename=log_file, level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

source_db_path = 'instance/sensors_data.db'
destination_db_path = 'instance/sorted_data.db'

os.makedirs(os.path.dirname(source_db_path), exist_ok=True)
os.makedirs(os.path.dirname(destination_db_path), exist_ok=True)


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    if isinstance(dbapi_connection, SQLite3Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.close()


source_engine = create_engine(f'sqlite:///{source_db_path}',
                              connect_args={'check_same_thread': False},
                              echo=False)

destination_engine = create_engine(f'sqlite:///{destination_db_path}',
                                   echo=False)


def update_and_sort_data(destination_db, source_db):
    with source_db.begin() as source_transaction:
        with destination_db.begin() as destination_transaction:
            check_and_create_last_update_table(destination_db)
            last_timestamp = get_last_update_time(destination_db)

            topics = source_db.execute(text("SELECT DISTINCT topic FROM temp_records")).fetchall()
            logging.debug(f"Topics: {topics}")

            for topic in topics:
                topic_name, max_timestamp = process_topic(destination_db, source_db, topic, last_timestamp)
                sort_topic_data(destination_db, topic_name)
                remove_duplicates(destination_db, topic_name)
                if max_timestamp is not None:
                    update_last_update_time(destination_db, max_timestamp)

    logging.debug(f"Last timestamp: {last_timestamp}")


def remove_duplicates(destination_db, topic_name):
    destination_db.execute(
        text(f"CREATE TABLE IF NOT EXISTS temp_{topic_name} (timestamp TEXT, value REAL);"))
    destination_db.execute(
        text(f"INSERT INTO temp_{topic_name} SELECT timestamp, value FROM (SELECT timestamp, value FROM {topic_name} GROUP BY timestamp);"))
    destination_db.execute(
        text(f"DROP TABLE {topic_name};"))
    destination_db.execute(
        text(f"ALTER TABLE temp_{topic_name} RENAME TO {topic_name};"))


def check_and_create_last_update_table(destination_db):
    destination_db.execute(text("CREATE TABLE IF NOT EXISTS last_update (timestamp TEXT)"))
    result = destination_db.execute(text("SELECT count(*) FROM last_update"))
    if result.fetchone()[0] == 0:
        destination_db.execute(text("INSERT INTO last_update (timestamp) VALUES (NULL)"))


def get_last_update_time(destination_db):
    last_timestamp = None
    result = destination_db.execute(text("SELECT max(timestamp) FROM last_update"))
    result = result.fetchone()
    if result:
        last_timestamp = result[0]
        # Convert to ISO 8601 format
        last_timestamp = datetime.fromisoformat(last_timestamp).replace(tzinfo=timezone.utc)
        # Check if last_timestamp is greater than current time
        if last_timestamp > datetime.now(timezone.utc):
            last_timestamp = datetime.now(timezone.utc)
        # Subtract 1 minute
        last_timestamp = last_timestamp - timedelta(minutes=1)
        last_timestamp = last_timestamp.isoformat()
    return last_timestamp


def process_topic(destination_db, source_db, topic, last_timestamp):
    topic_name = topic[0].replace('/', '_')
    destination_db.execute(
        text(f"CREATE TABLE IF NOT EXISTS {topic_name} (timestamp TEXT, value REAL)"))
    data = get_data_from_source(source_db, topic, last_timestamp)

    max_timestamp = None
    for row in data:
        timestamp, value = row
        destination_db.execute(
            text(f"INSERT INTO {topic_name} (timestamp, value) VALUES (:timestamp, :value)"),
            {"timestamp": timestamp, "value": value})
        if max_timestamp is None or timestamp > max_timestamp:
            max_timestamp = timestamp

    logging.debug(f"Data from source: {data}")

    return topic_name, max_timestamp


def get_data_from_source(source_db, topic, last_timestamp):
    if last_timestamp:
        # Convert last_timestamp to datetime object, add 1 second, and convert back to string
        last_timestamp = (datetime.fromisoformat(last_timestamp.replace('Z', '+00:00')) + timedelta(seconds=1)).isoformat()
        logging.debug(f"Last timestamp before query: {last_timestamp}")
        data = source_db.execute(
            text(f"SELECT timestamp, value FROM temp_records WHERE topic=:topic AND timestamp>=:last_timestamp"),
            {"topic": topic[0], "last_timestamp": last_timestamp}).fetchall()
    else:
        data = source_db.execute(
            text(f"SELECT timestamp, value FROM temp_records WHERE topic=:topic"),
            {"topic": topic[0]}).fetchall()
    logging.debug(f"Data from query: {data}")
    return data


def sort_topic_data(destination_db, topic_name):
    destination_db.execute(
        text(f"CREATE TABLE IF NOT EXISTS temp_{topic_name} (timestamp TEXT, value REAL);"))
    destination_db.execute(
        text(f"INSERT INTO temp_{topic_name} SELECT * FROM {topic_name} ORDER BY timestamp ASC;"))
    destination_db.execute(text(f"DROP TABLE {topic_name};"))
    destination_db.execute(
        text(f"ALTER TABLE temp_{topic_name} RENAME TO {topic_name};"))


def update_last_update_time(destination_db, max_timestamp):
    destination_db.execute(text("UPDATE last_update SET timestamp=:max_timestamp"),
                           {"max_timestamp": max_timestamp})


def delete_old_records(destination_db):
    tables = destination_db.execute(text("SELECT name FROM sqlite_master WHERE type='table';")).fetchall()
    cutoff_date = datetime.utcnow() - timedelta(days=180)
    cutoff_date_str = cutoff_date.strftime('%Y-%m-%d %H:%M:%S')

    for table in tables:
        table_name = table[0]
        if table_name != 'last_update':  # skip the last_update table
            destination_db.execute(
                text(f"DELETE FROM {table_name} WHERE timestamp < :cutoff_date_str;"),
                {"cutoff_date_str": cutoff_date_str})


def main():
    while True:
        try:
            with lock:
                with source_engine.connect() as source_db:
                    with destination_engine.connect() as destination_db:
                        destination_db.connection.execute("PRAGMA wal_checkpoint;")
                        update_and_sort_data(destination_db, source_db)
                        logging.debug("Finished updating and sorting data.")
                        destination_db.connection.execute("PRAGMA wal_checkpoint;")
                        delete_old_records(destination_db)
                        logging.debug("Finished old records deletion.")
        except Exception as e:
            logging.exception("Unexpected error occurred")
        try:
            time.sleep(60)
        except KeyboardInterrupt:
            return


if __name__ == "__main__":
    main()
