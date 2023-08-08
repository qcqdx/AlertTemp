import sqlite3

# Подключение к базе данных
conn = sqlite3.connect('../instance/user_settings.db')
cursor = conn.cursor()

# Добавление новых полей
cursor.execute("ALTER TABLE ranges ADD COLUMN critical_overheat INTEGER DEFAULT 0")
cursor.execute("ALTER TABLE ranges ADD COLUMN critical_overcool INTEGER DEFAULT 0")

# Закрыть соединение
conn.commit()
conn.close()
