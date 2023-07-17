import os
import logging
import threading
import time
from datetime import datetime, timedelta
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


def update_and_sort_data():
    source_db = source_engine.connect()
    destination_db = destination_engine.connect()

    with source_db.begin() as source_transaction:
        with destination_db.begin() as destination_transaction:
            check_and_create_last_update_table(destination_db)
            last_timestamp = get_last_update_time(destination_db)

            topics = source_db.execute(text("SELECT DISTINCT topic FROM temp_records")).fetchall()

            for topic in topics:
                topic_name = process_topic(destination_db, source_db, topic, last_timestamp)
                sort_topic_data(destination_db, topic_name)
                remove_duplicates(destination_db, topic_name)

            update_last_update_time(destination_db)


def remove_duplicates(destination_db, topic_name):
    destination_db.execute(
        text(f"""
        CREATE TABLE IF NOT EXISTS temp_{topic_name} (timestamp TEXT, value REAL);
        """)
    )
    destination_db.execute(
        text(f"""
        INSERT INTO temp_{topic_name} SELECT timestamp, value FROM (SELECT timestamp, value FROM {topic_name} GROUP BY timestamp);
        """)
    )
    destination_db.execute(
        text(f"""
        DROP TABLE {topic_name};
        """)
    )
    destination_db.execute(
        text(f"""
        ALTER TABLE temp_{topic_name} RENAME TO {topic_name};
        """)
    )


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
    return last_timestamp


def process_topic(destination_db, source_db, topic, last_timestamp):
    topic_name = topic[0].replace('/', '_')
    destination_db.execute(
        text(f"CREATE TABLE IF NOT EXISTS {topic_name} (timestamp TEXT, value REAL)"))
    data = get_data_from_source(source_db, topic, last_timestamp)

    for row in data:
        timestamp, value = row
        destination_db.execute(
            text(f"INSERT INTO {topic_name} (timestamp, value) VALUES (:timestamp, :value)"),
            {"timestamp": timestamp, "value": value})
    return topic_name


def get_data_from_source(source_db, topic, last_timestamp):
    if last_timestamp:
        data = source_db.execute(
            text(f"SELECT timestamp, value FROM temp_records WHERE topic=:topic AND timestamp>:last_timestamp"),
            {"topic": topic[0], "last_timestamp": last_timestamp}).fetchall()
    else:
        data = source_db.execute(
            text(f"SELECT timestamp, value FROM temp_records WHERE topic=:topic"),
            {"topic": topic[0]}).fetchall()
    return data


def sort_topic_data(destination_db, topic_name):
    destination_db.execute(
        text(f"CREATE TABLE IF NOT EXISTS temp_{topic_name} (timestamp TEXT, value REAL)"))
    destination_db.execute(
        text(f"INSERT INTO temp_{topic_name} SELECT * FROM {topic_name} ORDER BY timestamp ASC"))
    destination_db.execute(text(f"DROP TABLE {topic_name}"))
    destination_db.execute(
        text(f"ALTER TABLE temp_{topic_name} RENAME TO {topic_name}"))


def update_last_update_time(destination_db):
    current_time = time.strftime('%Y-%m-%d %H:%M:%S')
    destination_db.execute(text("UPDATE last_update SET timestamp=:current_time"),
                           {"current_time": current_time})


def delete_old_records(destination_db):
    tables = destination_db.execute(text("SELECT name FROM sqlite_master WHERE type='table';")).fetchall()
    cutoff_date = datetime.now() - timedelta(days=180)
    cutoff_date_str = cutoff_date.strftime('%Y-%m-%d %H:%M:%S')

    for table in tables:
        table_name = table[0]
        if table_name != 'last_update':  # skip the last_update table
            destination_db.execute(
                text(f"DELETE FROM {table_name} WHERE timestamp < :cutoff_date_str"), {"cutoff_date_str": cutoff_date_str})


while True:
    try:
        # Move engine creation inside the loop
        source_engine = create_engine(f'sqlite:///{source_db_path}',
                                      connect_args={'check_same_thread': False},
                                      echo=False)

        destination_engine = create_engine(f'sqlite:///{destination_db_path}',
                                           echo=False)

        with lock:
            logging.debug("Start updating and sorting data...")
            try:
                with destination_engine.connect() as conn:
                    conn.execute(text("PRAGMA wal_checkpoint;"))
                update_and_sort_data()
            except Exception as e:
                logging.exception("Error occurred during updating and sorting data")
            else:
                logging.debug("Finished updating and sorting data.")

            logging.debug("Starting old records deletion...")
            with destination_engine.connect().begin() as destination_transaction:
                try:
                    destination_transaction.connection.execute(text("PRAGMA wal_checkpoint;"))
                    delete_old_records(destination_transaction.connection)
                except Exception as e:
                    logging.exception("Error occurred during old records deletion")
                else:
                    logging.debug("Finished old records deletion.")
    except Exception as e:
        logging.exception("Unexpected error occurred")

    try:
        time.sleep(60)
    except KeyboardInterrupt:
        break
