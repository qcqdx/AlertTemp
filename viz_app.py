import os
import sqlite3

import pytz
from flask import Flask, g, render_template, send_from_directory, request, redirect, flash, url_for
from flask_bootstrap import Bootstrap
from dateutil.parser import parse
import pandas as pd
import json
import plotly.graph_objs as go
import plotly
from datetime import datetime, time

app = Flask(__name__)
bootstrap = Bootstrap(app)
app.secret_key = os.urandom(24)

DATABASE1 = os.path.join(os.path.dirname(__file__), 'instance', 'sorted_data.db')
DATABASE2 = os.path.join(os.path.dirname(__file__), 'instance', 'user_settings.db')


# Вспомогательные функции
def get_db(db_path):
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(db_path)
    return db


def get_user_db():
    db = getattr(g, '_user_database', None)
    if db is None:
        db = g._user_database = sqlite3.connect(DATABASE2)
        db.execute("CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, uname TEXT, userid TEXT, email TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS tabs (id INTEGER PRIMARY KEY, tab_name TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS ranges (tab_id INTEGER, overheat INTEGER, overcool INTEGER)")  # New line
    return db


def execute_with_retry(cursor, query, params=()):
    while True:
        try:
            cursor.execute(query, params)
            break  # Break the loop if the query was successful
        except sqlite3.OperationalError as e:
            if 'database is locked' in str(e):
                time.sleep(0.1)  # Wait a little before retrying
            else:
                raise  # Raise the exception if it's not a 'database is locked' error


def check_status(time):
    current_time = datetime.now(pytz.UTC)
    print(f"Time type: {type(time)}, Time value: {time}")
    time_difference = current_time - datetime.strptime(time, '%Y-%m-%dT%H:%M:%S.%f%z')
    return "Online" if time_difference.total_seconds() < 120 else "Offline"


def get_users():
    db = get_user_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users")
    users = cur.fetchall()
    return users


def get_table_names():
    cur = get_db(DATABASE1).cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cur.fetchall()

    table_names = []
    for table in tables:
        if table[0] == 'last_update':
            continue
        table_names.append(table[0])
    return table_names


def get_table_data():
    cur = get_db(DATABASE1).cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cur.fetchall()

    table_data = []
    user_cur = get_user_db().cursor()
    for table in tables:
        if table[0] == 'last_update':
            continue
        cur.execute(f"SELECT * FROM {table[0]} ORDER BY timestamp DESC LIMIT 1;")
        row = cur.fetchone()
        _, time, value = row  # здесь происходит изменение

        try:
            user_cur.execute("SELECT tab_name, table1, table1_alias, table2, table2_alias, table3, table3_alias "
                             "FROM tab_settings JOIN tabs ON tab_settings.tab_id = tabs.id "
                             "WHERE table1 = ? OR table2 = ? OR table3 = ?", (table[0], table[0], table[0]))
            tab_info = user_cur.fetchone()
        except sqlite3.OperationalError:
            tab_info = None

        if tab_info:
            tab_name, table1, table1_alias, table2, table2_alias, table3, table3_alias = tab_info
            aliases = {table1: table1_alias, table2: table2_alias, table3: table3_alias}
            alias = aliases.get(table[0], 'Unknown')
        else:
            tab_name = 'Unknown'
            alias = 'Unknown'

        table_data.append((table[0], alias, tab_name, time, check_status(time), value))
    return table_data


def get_tab_settings(tab_id):
    cur = get_db(DATABASE2).cursor()
    cur.execute("SELECT * FROM tab_settings WHERE tab_id = ?", (tab_id,))
    return cur.fetchone()


def create_plot(data):
    # Создайте график с использованием plotly
    data_to_plot = []

    for column in data.columns:
        if column != 'Время':
            data_to_plot.append(
                go.Scatter(
                    x=data['Время'],
                    y=data[column],
                    mode='lines',
                    name=column  # псевдонимы датчиков используются в качестве имён трасс
                )
            )

    graph_json = json.dumps(data_to_plot, cls=plotly.utils.PlotlyJSONEncoder)

    return graph_json


def get_incidents(tab_id):
    # print(f"Getting incidents for tab_id: {tab_id}")  # add this line

    # Create a direct connection to the database instead of using get_db
    db = sqlite3.connect('instance/incidents.db')
    cur = db.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='incidents';")
    if cur.fetchone() is not None:
        cur.execute("SELECT datetime, event, sensor, value FROM incidents WHERE tab_id=?", (tab_id,))
        incidents = cur.fetchall()
    else:
        incidents = []

    # Add these lines to see all unique tab_id values in the incidents table
    cur.execute("SELECT DISTINCT tab_id FROM incidents")
    unique_tab_ids = cur.fetchall()
    # print(f"Unique tab_id values in the incidents table: {unique_tab_ids}")

    return incidents


def get_all_tabs():
    db = get_user_db()  # Замените эту строку на код, который вы используете для получения своей базы данных
    cur = db.cursor()
    cur.execute("SELECT * FROM tabs")
    tabs = cur.fetchall()
    return tabs


# Обработчики маршрутов
@app.route('/')
def index():
    cur = get_user_db().cursor()
    cur.execute("SELECT * FROM tabs")
    tabs = cur.fetchall()

    data = {
        'table_data': get_table_data(),
        'tabs': tabs,
    }

    return render_template('index.html', data=data, tabs=get_all_tabs())


@app.route('/tabs/<int:tab_id>', methods=['GET', 'POST'])
def tab(tab_id):
    user_db = get_user_db()
    cur = user_db.cursor()

    if request.method == 'POST':
        cur.execute("SELECT * FROM ranges WHERE tab_id = ?", (tab_id,))
        existing_range = cur.fetchone()

        overheat = request.form['overheat']
        overcool = request.form['overcool']

        if existing_range:
            cur.execute("""
                UPDATE ranges
                SET overheat = ?, overcool = ?
                WHERE tab_id = ?
            """, (overheat, overcool, tab_id))
        else:
            cur.execute("""
                INSERT INTO ranges (tab_id, overheat, overcool)
                VALUES (?, ?, ?)
            """, (tab_id, overheat, overcool))

        user_db.commit()

        return redirect(url_for('tab', tab_id=tab_id))
    else:
        cur.execute("SELECT * FROM tabs")
        tabs = cur.fetchall()

        try:
            cur.execute("SELECT * FROM tab_settings WHERE tab_id = ?", (tab_id,))
            tab_settings = cur.fetchone()
            # print(f"tab_settings: {tab_settings}")
        except sqlite3.OperationalError as e:
            if 'no such table' in str(e):
                tabs = get_all_tabs()
                table_names = get_table_names()
                data = {'tabs': tabs}
                user_settings = None
                return render_template('new_tab.html', tab_id=tab_id, table_names=table_names, data=data,
                                       user_settings=user_settings)
            else:
                raise

        table_names = get_table_names()

        cur.execute("SELECT * FROM ranges WHERE tab_id = ?", (tab_id,))
        range_data = cur.fetchone()
        # print(f"range_data: {range_data}")

        if tab_settings is None or range_data is None:
            data = {'tabs': tabs}
            user_settings = None
            return render_template('new_tab.html', tab_id=tab_id, table_names=table_names, tab_settings=tab_settings,
                                   range_data=range_data, data=data, user_settings=user_settings)
        else:
            data = {
                'tabs': tabs,
            }

            user_settings = cur.fetchone()

            final_df = pd.DataFrame()
            data_tables = tab_settings[1:6:2]
            data_table_aliases = tab_settings[2:7:2]
            data_db = get_db('instance/sorted_data.db')
            cur2 = data_db.cursor()

            for idx, (table, alias) in enumerate(zip(data_tables, data_table_aliases)):
                cur2.execute(f"SELECT * FROM {table} ORDER BY timestamp DESC;")
                data = cur2.fetchall()
                timestamps = [row[1] for row in data]
                values = [row[2] for row in data]

                temp_df = pd.DataFrame({'Время': timestamps, alias: values})
                temp_df['Время'] = pd.to_datetime(temp_df['Время'], format='ISO8601').dt.round('1s')

                if final_df.empty:
                    ids = [row[0] for row in data]  # добавляем столбец ID только один раз
                    final_df = pd.DataFrame({'ID': ids, 'Время': timestamps})
                    final_df['Время'] = pd.to_datetime(final_df['Время']).dt.round('1s')
                final_df = final_df.iloc[::-1]
                # Объединяем основной DataFrame с данными текущего датчика
                final_df = pd.merge(final_df, temp_df, on='Время', how='outer')

            cur2.close()

            final_df.set_index('Время', inplace=True)
            final_df.sort_values(by='ID', ascending=False, inplace=True)
            # print(final_df)

            try:
                final_df.index = final_df.index.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
            except AttributeError as e:
                print(f"AttributeError: {e}. Probably the DataFrame is empty or its index is not a DatetimeIndex.")

            plot_data = []
            overheat = range_data[1]
            overcool = range_data[2]

            # color_list = ['#2E8B57', '#808000', '#B87333']  # Морская волна, Оливковый, Медь
            # color_list = ['#800080', '#006400', '#8B4513']  # Пурпурный, Темно-зеленый, Коричневый
            color_list = ['#FDB813', '#808000', '#00FFFF']  # Золотистый, Оливковый, Циан

            for i, alias in enumerate(data_table_aliases):
                curve = {
                    'x': final_df.index.tolist(),
                    'y': final_df[alias].tolist(),
                    'type': 'scatter',  # тип графика
                    'name': alias,  # название кривой
                    'line': {'color': color_list[i]}  # индивидуальный цвет для каждой линии
                }
                plot_data.append(curve)

                overheat_line = {
                    'x': [final_df.index.min(), final_df.index.max()],
                    'y': [overheat, overheat],
                    'mode': 'lines',
                    'name': 'Перегрев',
                    'showlegend': False,  # Не отображать в легенде
                    'line': {
                        'color': 'red',
                        'width': 1,
                        'dash': 'dash'
                    }
                }
                plot_data.append(overheat_line)

                overcool_line = {
                    'x': [final_df.index.min(), final_df.index.max()],
                    'y': [overcool, overcool],
                    'mode': 'lines',
                    'name': 'Переохлаждение',
                    'showlegend': False,  # Не отображать в легенде
                    'line': {
                        'color': 'blue',
                        'width': 1,
                        'dash': 'dash'
                    }
                }
                plot_data.append(overcool_line)

            incidents = get_incidents(tab_id)
            # print(f"incidents: {incidents}")

            # print(tabs)

            return render_template('tab.html', tab_id=tab_id, table_names=table_names, tabs=get_all_tabs(), data=data,
                                   tab_settings=tab_settings, user_settings=user_settings, range_data=range_data,
                                   final_df=final_df.reset_index().to_dict(orient='records'),
                                   plot_data=json.dumps(plot_data), overheat=overheat, overcool=overcool,
                                   incidents=incidents)


@app.route('/save_tab_settings', methods=['POST'])
def save_tab_settings():
    tab_id = request.form['tab_id']
    table1 = request.form['table1']
    table1_alias = request.form['table1_alias']
    table2 = request.form['table2']
    table2_alias = request.form['table2_alias']
    table3 = request.form['table3']
    table3_alias = request.form['table3_alias']

    # Добавляем проверку на одинаковые значения датчиков
    if len({table1, table2, table3}) < 3:
        flash('Выберите разные датчики.', 'warning')  # Используем 'warning' в качестве категории сообщения
        return redirect(url_for('tab', tab_id=tab_id))  # Возвращаем пользователя обратно на страницу

    db = get_user_db()
    cur = db.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='tab_settings';")
    if not cur.fetchone():
        cur.execute("""
            CREATE TABLE tab_settings (
                tab_id INTEGER PRIMARY KEY,
                table1 TEXT,
                table1_alias TEXT,
                table2 TEXT,
                table2_alias TEXT,
                table3 TEXT,
                table3_alias TEXT
            )
        """)

    # Проверяем, существуют ли поля alias в таблице
    cur.execute("PRAGMA table_info(tab_settings)")
    columns = cur.fetchall()
    columns_names = [column[1] for column in columns]

    if "table1_alias" not in columns_names:
        # Создаем новую временную таблицу
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tab_settings_temp (
                tab_id INTEGER PRIMARY KEY,
                table1 TEXT,
                table1_alias TEXT,
                table2 TEXT,
                table2_alias TEXT,
                table3 TEXT,
                table3_alias TEXT
            )
        """)
        # Копируем данные в новую таблицу
        cur.execute(
            "INSERT INTO tab_settings_temp (tab_id, table1, table2, table3) "
            "SELECT tab_id, table1, table2, table3 FROM tab_settings"
        )
        # Удаляем старую таблицу
        cur.execute("DROP TABLE tab_settings")
        # Переименовываем временную таблицу в оригинальное название
        cur.execute("ALTER TABLE tab_settings_temp RENAME TO tab_settings")

    # Проверяем, существуют ли уже настройки для этой вкладки
    cur.execute("SELECT * FROM tab_settings WHERE tab_id = ?", (tab_id,))
    if cur.fetchone():
        # Если существуют, обновляем их
        cur.execute("""
            UPDATE tab_settings
            SET table1 = ?, table1_alias = ?, table2 = ?, table2_alias = ?, table3 = ?, table3_alias = ?
            WHERE tab_id = ?
        """, (table1, table1_alias, table2, table2_alias, table3, table3_alias, tab_id))
    else:
        # Иначе создаем новую запись
        cur.execute("""
            INSERT INTO tab_settings (tab_id, table1, table1_alias, table2, table2_alias, table3, table3_alias)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (tab_id, table1, table1_alias, table2, table2_alias, table3, table3_alias))

    db.commit()

    flash('Настройки сохранены.', 'success')
    return redirect(url_for('tab', tab_id=tab_id))


@app.route('/save_ranges', methods=['POST'])
def save_ranges():
    tab_id = request.form['tab_id']
    overheat = request.form['overheat']
    overcool = request.form['overcool']
    # print(f"Overheat: {overheat}, Overcool: {overcool}")
    db = get_user_db()
    cur = db.cursor()

    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ranges';")
    if not cur.fetchone():
        cur.execute("""
            CREATE TABLE ranges (
                tab_id INTEGER PRIMARY KEY,
                overheat INTEGER,
                overcool INTEGER
            )
        """)

    cur.execute("SELECT * FROM ranges WHERE tab_id = ?", (tab_id,))
    if cur.fetchone():
        cur.execute("""
            UPDATE ranges
            SET overheat = ?, overcool = ?
            WHERE tab_id = ?
        """, (overheat, overcool, tab_id))
    else:
        cur.execute("""
            INSERT INTO ranges (tab_id, overheat, overcool)
            VALUES (?, ?, ?)
        """, (tab_id, overheat, overcool))

    db.commit()

    flash('Диапазоны сохранены.', 'success')
    return redirect(url_for('tab', tab_id=tab_id))


@app.route('/parameters')
def parameters():
    cur = get_user_db().cursor()
    cur.execute("SELECT * FROM tabs")
    tabs = cur.fetchall()

    users = get_users()

    data = {
        'tabs': tabs,
    }

    # Get the list of bots and their names from the database
    cur.execute("SELECT name, api_key FROM bots")
    bots = cur.fetchall()

    return render_template('parameters.html', users=users, data=data, bots=bots, tabs=get_all_tabs())


@app.route('/add_user', methods=['POST'])
def add_user():
    uname = request.form.get('uname')
    userid = request.form.get('userid')
    email = request.form.get('email')

    db = get_user_db()
    cur = db.cursor()
    cur.execute("INSERT INTO users (uname, userid, email) VALUES (?, ?, ?)", (uname, userid, email))
    db.commit()

    return redirect('/parameters')


@app.route('/add_bot', methods=['POST'])
def add_bot():
    # Get the bot name and API key from the form
    bname = request.form.get('bname')
    bkey = request.form.get('bkey')

    # Connect to the database
    conn = sqlite3.connect('instance/user_settings.db')
    cur = conn.cursor()

    # Check if the bots table exists and create it if it does not
    cur.execute("CREATE TABLE IF NOT EXISTS bots (name text, api_key text)")

    # Insert the new bot into the database
    cur.execute("INSERT INTO bots (name, api_key) VALUES (?, ?)", (bname, bkey))
    conn.commit()

    # Flash a message to let the user know the bot has been added
    flash("Бот успешно добавлен!")

    # Redirect to the home page
    return redirect('/parameters')


@app.route('/delete_bot', methods=['POST'])
def delete_bot():
    # Get the bot name from the form
    bname = request.form.get('bot_name')

    # Connect to the database
    conn = sqlite3.connect('instance/user_settings.db')
    cur = conn.cursor()

    # Delete the bot from the database
    cur.execute("DELETE FROM bots WHERE name = ?", (bname,))
    conn.commit()

    # Flash a message to let the user know the bot has been deleted
    flash("Бот успешно удален!")

    # Redirect to the home page
    return redirect('/parameters')


@app.route('/add_tab', methods=['POST'])
def add_tab():
    tab_name = request.form.get('tab_name')

    db = get_user_db()
    cur = db.cursor()
    cur.execute("INSERT INTO tabs (tab_name) VALUES (?)", (tab_name,))
    db.commit()

    return redirect('/')  # или редирект на другую страницу, если нужно


@app.route('/rename_tab', methods=['POST'])
def rename_tab():
    new_tab_name = request.form.get('new_tab_name')
    tab_id = request.form.get('tab_id')

    db = get_user_db()
    cur = db.cursor()
    cur.execute("UPDATE tabs SET tab_name = ? WHERE id = ?", (new_tab_name, tab_id))
    db.commit()

    return redirect(url_for('tab', tab_id=tab_id))  # перенаправление на обновленную вкладку


@app.route('/delete_tab', methods=['POST'])
def delete_tab():
    tab_id = request.form.get('tab_id')

    db = get_user_db()
    cur = db.cursor()

    # Удаляем связанные настройки вкладки
    cur.execute("DELETE FROM tab_settings WHERE tab_id=?", (tab_id,))
    cur.execute("DELETE FROM ranges WHERE tab_id=?", (tab_id,))  # New line

    # Удаляем саму вкладку
    cur.execute("DELETE FROM tabs WHERE id=?", (tab_id,))

    db.commit()

    return redirect('/')  # или редирект на другую страницу, если нужно


@app.route('/delete_user', methods=['POST'])
def delete_user():
    user_id = request.form.get('user_id')

    db = get_user_db()
    cur = db.cursor()
    cur.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()

    return redirect('/parameters')


@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico',
                               mimetype='image/vnd.microsoft.icon')


@app.template_filter('formatdatetime')
def format_datetime(value):
    if value is None:
        return ""

    dt = parse(value)
    return dt.strftime('%Y-%m-%d %H:%M:%S')


if __name__ == '__main__':
    env = os.getenv('FLASK_ENV', 'development')
    try:
        if env == 'production':
            app.run(host='0.0.0.0')
        else:
            app.run()
    except KeyboardInterrupt:
        pass
