from sqlalchemy import create_engine, text

destination_db_path = '../instance/sorted_data.db'
destination_engine = create_engine(f'sqlite:///{destination_db_path}', echo=False)

def set_last_update_time(destination_db):
    destination_db.execute(text("UPDATE last_update SET timestamp='2023-07-17T00:00:00+00:00'"))

with destination_engine.connect() as destination_db:
    with destination_db.begin():
        set_last_update_time(destination_db)
