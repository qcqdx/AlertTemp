import os
import sqlite3

for dirpath, dirnames, filenames in os.walk('..'):
    for file in filenames:
        if file.endswith('.db'):
            db_file = os.path.join(dirpath, file)
            conn = sqlite3.connect(db_file)
            c = conn.cursor()

            c.execute("SELECT name FROM sqlite_master WHERE type='table';")
            print(f'Tables in {db_file}:')
            tables = c.fetchall()
            for table in tables:
                print(table[0])

            for table in tables:
                print(f'\nTable structure for {table[0]} in {db_file}:')
                c.execute(f'PRAGMA table_info({table[0]});')
                rows = c.fetchall()
                for row in rows:
                    print(row)

                print(f'\nFirst 10 entries for {table[0]} in {db_file}:')
                c.execute(f'SELECT * FROM {table[0]} LIMIT 10;')
                rows = c.fetchall()
                for row in rows:
                    print(row)

                print(f'\nLast 10 entries for {table[0]} in {db_file}:')
                c.execute(f'SELECT * FROM {table[0]} ORDER BY ROWID DESC LIMIT 10;')
                rows = c.fetchall()
                for row in reversed(rows):
                    print(row)

            conn.close()
