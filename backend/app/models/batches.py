"""Партии препаратов (концепция §6.3, этап B.1).

Партия — отметка персонала «что и когда помещено в холодильник»: с даты
загрузки бюджет стабильности и MKT считаются ОТ ПАРТИИ, а не от скользящего
окна — вопрос «можно ли применять препарат» относится к конкретной партии,
а не к последним суткам.

Партия — метка периода поверх измерений, собственной истории у неё нет
(измерения и инциденты на неё не ссылаются), поэтому, в отличие от
устройств (паттерн ROADMAP §2.13), допускается DELETE ошибочно заведённой
записи — история температур при этом не затрагивается.
"""

from datetime import datetime

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UTCDateTime


class Batch(TimestampMixin, Base):
    __tablename__ = "batch"

    id: Mapped[int] = mapped_column(primary_key=True)
    controller_id: Mapped[int] = mapped_column(
        ForeignKey("controller.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # человекочитаемая метка: препарат, серия, накладная
    label: Mapped[str] = mapped_column(String(200), nullable=False)
    loaded_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    # NULL = партия ещё в холодильнике
    unloaded_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    notes: Mapped[str | None] = mapped_column(Text)
