"""Разбор неоднородных адресов из CSV в (город, улица, дом) для геокодера.

Встречающиеся формы:
  Город Москва, пр-кт.Волгоградский, д. 128 к 5
  г.Город Москва, наб.Семеновская, д. 3/1к2
  Домодедово, проезд.Советский 1-й, д. 1А
  обл.Московская область, г.Домодедово, пгт.Востряково-1, ул.Жуковского, д. 14/18
  МО, г. Кашира Кржижановского ул. д. 7к2
  Москва Булатниковский пр-зд. д. 6к1
  г. Москва, ул Юных Ленинцев, д 83с 4
"""
from __future__ import annotations

import re
from dataclasses import dataclass

CITIES = ("Домодедово", "Кашира", "Ступино", "Москва")

STREET_TYPES = {
    "ул": "улица",
    "пр-кт": "проспект",
    "б-р": "бульвар",
    "пер": "переулок",
    "проезд": "проезд",
    "пр-зд": "проезд",
    "наб": "набережная",
    "ш": "шоссе",
}

_HOUSE_RE = re.compile(r"[,\s]д\.?\s*(?P<house>[0-9][0-9А-Яа-я/ ]*?(?:\s*стр\.\s*\d+)?)\s*$")
_STREET_TYPE_RE = re.compile(r"(?<![\w-])(" + "|".join(re.escape(t) for t in STREET_TYPES) + r")(?![\w-])\.?")
_NOISE_RE = re.compile(
    r"обл\.\s*Московская область|(?<!\w)МО(?!\w)|г\.\s*Город|Город|(?<!\w)г\.|пгт\.\s*[\w-]+|"
    + "|".join(CITIES)
)


@dataclass(frozen=True)
class ParsedAddress:
    city: str
    street_type: str | None
    street_name: str
    house: str | None

    @property
    def street(self) -> str:
        return f"{self.street_name} {self.street_type}" if self.street_type else self.street_name


def normalize_house(house: str) -> str:
    """'128 к 5' -> '128к5', '83с 4' -> '83с4', '28 стр. 1' -> '28с1'."""
    house = re.sub(r"стр\.", "с", house)
    return re.sub(r"\s+", "", house)


def parse_address(address: str) -> ParsedAddress:
    text = address.replace("ё", "е").replace("Ё", "Е")
    city = next((c for c in CITIES if c in text), "Москва")

    house = None
    m = _HOUSE_RE.search(text)
    if m:
        house = normalize_house(m.group("house"))
        text = text[: m.start()]

    street_type = None
    m = _STREET_TYPE_RE.search(text)
    if m:
        street_type = STREET_TYPES[m.group(1)]
        text = text[: m.start()] + " " + text[m.end():]

    text = _NOISE_RE.sub(" ", text)
    street_name = re.sub(r"[\s,.]+", " ", text).strip()
    return ParsedAddress(city=city, street_type=street_type, street_name=street_name, house=house)
