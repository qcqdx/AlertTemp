import os
import logging
import configparser
import sqlite3
import threading
import time
from datetime import datetime, timedelta
import random
import signal
import sys

import pytz
import paho.mqtt.client as mqtt_client


def load_config(config_file):
    config_data = configparser.ConfigParser()
    if os.path.exists(config_file):
        config_data.read(config_file)
    else:
        # create default config
        config_data["mqtt"] = {
            "broker": "10.10.0.3",
            "port": "1883",
        }
        config_data["database"] = {
            "name": "instance/sensors_data.db"
        }
        os.makedirs(os.path.dirname(config_file), exist_ok=True)
        with open(config_file, 'w') as configfile:
            config_data.write(configfile)
    return config_data


# Load configuration from the file
config = load_config(os.path.join('instance', 'data-collector.conf'))

# Use configuration variables
mqtt_broker = config.get('mqtt', 'broker', fallback="localhost")
mqtt_port = config.getint('mqtt', 'port', fallback=1883)
db_name = config.get('database', 'name', fallback="instance/sensors_data.db")


initial_timestamp = datetime.now(pytz.UTC).isoformat()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s: %(message)s',
    handlers=[logging.FileHandler("instance/data-collector.log", 'a', 'utf-8')]
)
logging.info('Application started')


class MQTTClient:
    def __init__(self, broker, port, max_retries=3):
        self.client = mqtt_client.Client()
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = MQTTClient.on_disconnect
        self.client.on_message = self.on_message
        self.broker = broker
        self.port = port
        self.data_buffer = []
        self.max_retries = max_retries

    def on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logging.info(f"Connected to MQTT broker {self.broker}:{self.port}")
            self.client.subscribe("#")
        else:
            logging.error("Connection failed")

    @staticmethod
    def on_disconnect(client, userdata, rc):
        if rc != 0:
            logging.warning("Unexpected disconnection")

    def on_message(self, client, userdata, message):
        try:
            topic = message.topic
            value = float(message.payload.decode("utf-8"))
            msg_timestamp = datetime.now(pytz.UTC).isoformat()
            self.data_buffer.append((msg_timestamp, topic, value))
        except ValueError as ve:
            logging.error(f"Could not convert MQTT message to float: {str(ve)}")
        except Exception as e:
            logging.error(f"Unexpected error while processing MQTT message: {str(e)}")

    def connect(self):
        retries = 0
        while retries < self.max_retries:
            try:
                self.client.connect(self.broker, self.port)
                return
            except Exception as e:
                retries += 1
                delay = pow(2, retries) + random.random()
                logging.error(f"Failed to connect to MQTT broker: {str(e)}. Retry attempt {retries} in {delay} seconds")
                time.sleep(delay)
            finally:
                self.client.loop_stop()

        logging.error(f"Failed to connect to MQTT broker after {self.max_retries} attempts. Exiting.")
        sys.exit(1)

    def loop_start(self):
        self.client.loop_start()

    def loop_stop(self):
        self.client.loop_stop()

    def disconnect(self):
        self.client.disconnect()

    def fetch_and_clear_data(self):
        data = self.data_buffer[:]
        self.data_buffer.clear()
        return data


class DatabaseManager:
    def __init__(self, database_name, max_retries=3):
        self.db_name = database_name
        self.max_retries = max_retries

    def __enter__(self):
        self.conn = sqlite3.connect(self.db_name, isolation_level=None)
        self.conn.execute("pragma journal_mode=wal")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.conn.close()

    def execute_with_retry(self, sql, params=()):
        retries = 0
        while retries < self.max_retries:
            try:
                self.conn.execute(sql, params)
                self.conn.commit()
                return
            except Exception as e:
                logging.error(f"Failed to execute SQL query: {str(e)}")
                retries += 1
                time.sleep(5)

        logging.error(f"Failed to execute SQL query after {self.max_retries} attempts. Exiting.")
        sys.exit(1)

    def initialize(self):
        with self:
            self.execute_with_retry(
                "CREATE TABLE IF NOT EXISTS temp_records (timestamp, topic, value)"
            )

    def insert(self, db_timestamp, topic, value):
        self.execute_with_retry(
            "INSERT INTO temp_records (timestamp, topic, value) VALUES (?, ?, ?)",
            (db_timestamp, topic, value)
        )

    def delete_old(self):
        six_months_ago = (datetime.now(pytz.UTC) - timedelta(days=180)).isoformat()
        self.execute_with_retry(
            "DELETE FROM temp_records WHERE timestamp < ?",
            (six_months_ago,)
        )


class DataProcessor(threading.Thread):
    def __init__(self, mqtt, database_name, max_buffer_size=500):
        super().__init__()
        self.mqtt = mqtt
        self.db_name = database_name
        self.max_buffer_size = max_buffer_size
        self.running = True

    def run(self):
        data = []
        last_flush_time = datetime.now(pytz.UTC)
        while self.running:
            time.sleep(1)
            if not self.running:
                break
            try:
                data.extend(self.mqtt.fetch_and_clear_data())
                if len(data) >= self.max_buffer_size or (datetime.now(pytz.UTC) - last_flush_time).seconds >= 60:
                    self.flush_data(data)
                    last_flush_time = datetime.now(pytz.UTC)
                    data.clear()
            except Exception as e:
                logging.error(f"Failed to process data: {str(e)}")

    def flush_data(self, data):
        topic_values = {}
        for msg_timestamp, topic, value in data:
            if topic not in topic_values:
                topic_values[topic] = []
            topic_values[topic].append(value)

        try:
            with DatabaseManager(self.db_name) as db_manager:
                for topic, values in topic_values.items():
                    avg_value = round(sum(values) / len(values), 2)
                    db_timestamp = datetime.now(pytz.UTC).isoformat()
                    db_manager.insert(db_timestamp, topic, avg_value)
                    logging.debug(f"Saved average data: topic={topic}, value={avg_value}, timestamp={db_timestamp}")
        except sqlite3.OperationalError as e:
            logging.error(f"Failed to flush data to database: {str(e)}")

    def stop(self):
        self.running = False


class DataCleaner(threading.Thread):
    def __init__(self, database_name):
        super().__init__()
        self.db_name = database_name
        self.running = True

    def run(self):
        while self.running:
            for _ in range(24 * 60 * 60):
                time.sleep(1)
                if not self.running:
                    return
            try:
                with DatabaseManager(self.db_name) as local_db_manager:
                    local_db_manager.delete_old()
                    logging.info("Old data cleaned successfully")
            except Exception as e:
                logging.error(f"Failed to clean old data: {str(e)}")

    def stop(self):
        self.running = False


def main():
    mqtt = MQTTClient(mqtt_broker, mqtt_port)
    mqtt.connect()

    with DatabaseManager(db_name) as db_manager:
        db_manager.initialize()

    data_processor = DataProcessor(mqtt, db_name)
    data_processor.start()

    data_cleaner = DataCleaner(db_name)
    data_cleaner.start()

    mqtt.loop_start()

    signal.signal(signal.SIGTERM, lambda signum, frame: shutdown(mqtt, data_processor, data_cleaner))
    signal.signal(signal.SIGINT, lambda signum, frame: shutdown(mqtt, data_processor, data_cleaner))
    signal.pause()


def shutdown(mqtt, data_processor, data_cleaner):
    mqtt.loop_stop()
    mqtt.disconnect()
    data_processor.stop()
    data_processor.join()
    data_cleaner.stop()
    data_cleaner.join()
    logging.info('Application stopped')


if __name__ == "__main__":
    main()
