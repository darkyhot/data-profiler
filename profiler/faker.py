"""Детерминированный фолбэк-фейкер (без внешних зависимостей).

Используется, когда LLM отключён или не смог сгенерить пул для чувствительной
колонки. Значения ЯВНО синтетические («не похожи на настоящие»), но
правдоподобные по формату — чтобы SQL-скрипты (LIKE, длины, JOIN по типу)
вели себя как на реальных данных.

Детерминизм по seed → воспроизводимые сэмплы между прогонами.
"""

from __future__ import annotations

import random

_FAM = ["Тестов", "Примеров", "Образцов", "Демидемо", "Синтетов", "Фейков", "Пробин",
        "Заглушкин", "Мокин", "Сэмплов", "Нулин", "Икслов"]
_NAME_M = ["Иван", "Пётр", "Сергей", "Алексей", "Дмитрий", "Тест", "Демо"]
_NAME_F = ["Мария", "Ольга", "Анна", "Елена", "Ирина", "Тестина", "Демина"]
_PATR_M = ["Иванович", "Петрович", "Сергеевич", "Тестович", "Демович"]
_PATR_F = ["Ивановна", "Петровна", "Сергеевна", "Тестовна", "Демовна"]
_ORG_FORM = ["ООО", "АО", "ПАО", "ИП"]
_ORG_NAME = ["Ромашка", "Пример", "Тест", "Заглушка", "Синтетика", "Демо", "Образец", "Мокап"]
_STREETS = ["Тестовая", "Примерная", "Образцовая", "Демонстрационная", "Синтетическая"]
_DOMAINS = ["example.test", "sample.local", "demo.invalid", "fake.test"]


class Faker:
    def __init__(self, seed: int = 42):
        self.r = random.Random(seed)

    # ── по категории чувствительности ────────────────────────────────────────
    def value(self, kind: str, idx: int) -> str:
        fn = {
            "inn": self.inn, "snils": self.snils, "passport": self.passport,
            "fio": self.fio, "person": self.fio, "phone": self.phone,
            "email": self.email, "account": self.account, "address": self.address,
            "birth": self.birth, "geo": self.geo, "money": self.money,
        }.get(kind)
        return fn(idx) if fn else f"SYN-{idx:06d}"

    def inn(self, idx: int) -> str:
        # 10 цифр, но заведомо «ненастоящий» префикс 00
        return "00" + "".join(str(self.r.randint(0, 9)) for _ in range(8))

    def snils(self, idx: int) -> str:
        d = "".join(str(self.r.randint(0, 9)) for _ in range(9))
        return f"{d[:3]}-{d[3:6]}-{d[6:9]} 00"

    def passport(self, idx: int) -> str:
        return f"00{self.r.randint(0,99):02d} {self.r.randint(0,999999):06d}"

    def fio(self, idx: int) -> str:
        if self.r.random() < 0.5:
            return f"{self.r.choice(_FAM)} {self.r.choice(_NAME_M)} {self.r.choice(_PATR_M)}"
        fam = self.r.choice(_FAM) + "а"
        return f"{fam} {self.r.choice(_NAME_F)} {self.r.choice(_PATR_F)}"

    def org(self, idx: int) -> str:
        return f'{self.r.choice(_ORG_FORM)} "{self.r.choice(_ORG_NAME)}-{idx:04d}"'

    def phone(self, idx: int) -> str:
        return f"+7000{self.r.randint(0, 9999999):07d}"

    def email(self, idx: int) -> str:
        return f"user{idx:05d}@{self.r.choice(_DOMAINS)}"

    def account(self, idx: int) -> str:
        return "40000" + "".join(str(self.r.randint(0, 9)) for _ in range(15))

    def address(self, idx: int) -> str:
        return (f"г. Тестоград, ул. {self.r.choice(_STREETS)}, "
                f"д. {self.r.randint(1, 200)}, кв. {self.r.randint(1, 300)}")

    def birth(self, idx: int) -> str:
        y = self.r.randint(1960, 2005)
        return f"{y:04d}-{self.r.randint(1,12):02d}-{self.r.randint(1,28):02d}"

    def geo(self, idx: int) -> float:
        return round(self.r.uniform(-90, 90), 6)

    def money(self, idx: int) -> float:
        return round(self.r.uniform(0, 1_000_000), 2)

    def pool(self, kind: str, size: int) -> list:
        return [self.value(kind, i) for i in range(size)]
