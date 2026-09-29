"""Справочники из ТЗ §2.4.1 и правила вывода атрибутов заявки из исходных CSV.

Все правила — допущения команды (в данных их нет), см. README §9.4.
"""
from __future__ import annotations

from enum import IntEnum
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATASET_DIR = ROOT / "dataset"
DATA_DIR = ROOT / "data"  # кэши геокодера/матриц/линий: не в git, восстанавливаются в рантайме
ENGINEERS_DIR = DATASET_DIR / "engineers"  # справочник инженеров базовых участков (в git)


class Skill(IntEnum):
    LOCAL = 1
    CONNECTION = 2
    EMERGENCY = 3

    @property
    def label(self) -> str:
        return SKILL_LABELS[self]


SKILL_LABELS = {
    Skill.LOCAL: "Локальные работы",
    Skill.CONNECTION: "Работы на подключение и дозаказы",
    Skill.EMERGENCY: "Аварийные работы",
}


class Transport(IntEnum):
    CAR = 1
    FOOT = 2
    BIKE = 3
    PUBLIC = 4

    @property
    def label(self) -> str:
        return TRANSPORT_LABELS[self]


TRANSPORT_LABELS = {
    Transport.CAR: "Автомобиль",
    Transport.FOOT: "Пешеход",
    Transport.BIKE: "Велосипед",
    Transport.PUBLIC: "Общественный транспорт",
}


class Priority(IntEnum):
    NORMAL = 1
    URGENT = 2

    @property
    def label(self) -> str:
        return "Срочная" if self is Priority.URGENT else "Обычная"


# Участок -> (название, файл с синтетическими данными в dataset/). Контрольные файлы dataset/control/ не используются.
AREAS: dict[str, tuple[str, str]] = {
    "vostok": ("Восток", "vostok.csv"),
    "yugo_vostok": ("Юго-восток", "yugo_vostok.csv"),
    "yugo_centr": ("Югоцентр", "yugo_centr.csv"),
}

BK_TO_SKILL = {
    "Локальная заявка": Skill.LOCAL,
    "Подключение": Skill.CONNECTION,
    "Дозаказ": Skill.CONNECTION,
    "Глобальная проблема": Skill.EMERGENCY,
}

# Длительность работ по типу HD, минуты.
HD_DURATION = {
    "Заявка на подключение": 90,
    "Конвергенция абонента": 60,
    "Заказ подключения/Дозаказ оборудования": 45,
    "Дозаказ оборудования": 45,
    "Переключение на Гбит/с": 45,
    "Нет линка": 45,
    "Разрывы": 45,
    "Рост ошибок на порту": 45,
    "Низкая скорость": 45,
    "IP-адрес 169...": 45,
    "Работа с кабелем": 60,
    "Роутер. Замена техническим специалистом": 30,
    "TVE/ENT. Замена приставки техником": 30,
    "ТВ. Замена приставки техником": 30,
    "TVE/ENT. Другие ошибки": 30,
    "Информация": 20,
    "Мониторинг": 20,
    "Авария": 120,
}
DEFAULT_DURATION = 45
EMERGENCY_DURATION = 120
GIGABIT_EXTRA_MINUTES = 15

# Типы работ, для которых нужен автомобиль (везти оборудование/кабель).
CAR_REQUIRED_HD = {"Авария", "Работа с кабелем"}


def skill_for(type_bk: str) -> Skill:
    return BK_TO_SKILL[type_bk]


def duration_for(type_bk: str, type_hd: str | None, gigabit: bool) -> int:
    if type_hd:
        base = HD_DURATION.get(type_hd, DEFAULT_DURATION)
    else:
        base = EMERGENCY_DURATION if skill_for(type_bk) is Skill.EMERGENCY else DEFAULT_DURATION
    return base + (GIGABIT_EXTRA_MINUTES if gigabit else 0)


def priority_for(type_bk: str) -> Priority:
    return Priority.URGENT if skill_for(type_bk) is Skill.EMERGENCY else Priority.NORMAL


def is_moscow(address: str) -> bool:
    return "Москва" in address and not address.startswith(("МО", "обл."))


def required_transport_for(type_bk: str, type_hd: str | None, address: str) -> Transport | None:
    if skill_for(type_bk) is Skill.EMERGENCY or type_hd in CAR_REQUIRED_HD:
        return Transport.CAR
    if not is_moscow(address):
        return Transport.CAR
    return None
