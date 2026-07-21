"""Детерминированный фолбэк-фейкер (без внешних зависимостей).

Используется, когда LLM отключён или не смог сгенерить пул для чувствительной
колонки. Значения ЯВНО синтетические («не похожи на настоящие»), но
правдоподобные по формату — чтобы SQL-скрипты (LIKE, длины, JOIN по типу)
вели себя как на реальных данных.

Детерминизм по seed → воспроизводимые сэмплы между прогонами.
"""

from __future__ import annotations

import random
import re

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


# Паттерны встроенной PII для маскировки КОМПОЗИТНЫХ полей
# (напр. author_login = "1212423 Иванов Иван Иванович [test@omega.sbrf.ru]").
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.U)
_FIO3_RE = re.compile(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+", re.U)
_FIO_INIT_RE = re.compile(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s?[А-ЯЁ]\.", re.U)
_DIGITS_RE = re.compile(r"\d{5,}")


class Faker:
    def __init__(self, seed: int = 42):
        self.r = random.Random(seed)
        self._c = 0                       # счётчик для детерминированной уникальности
        self._mask_cache: dict[str, str] = {}   # одно и то же реальное → тот же фейк (для join)

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

    # ── маскировка КОМПОЗИТНЫХ значений (PII внутри структуры) ────────────────
    def mask_text(self, s) -> str:
        """Вырезать PII из строки, сохранив структуру: email → фейк-email,
        ФИО (3 слова / «Фамилия И.О.») → фейк-ФИО, длинные числа → случайные.
        Остальное (скобки, разделители, id-формат) сохраняется. Кэш → одно и то
        же реальное значение всегда даёт тот же фейк (не ломает join по полю)."""
        s = "" if s is None else str(s)
        if s in self._mask_cache:
            return self._mask_cache[s]
        out = _DIGITS_RE.sub(lambda m: self._digits(len(m.group())), s)
        out = _FIO3_RE.sub(lambda m: self._fio_plain(), out)
        out = _FIO_INIT_RE.sub(lambda m: self._fio_initials(), out)
        out = _EMAIL_RE.sub(lambda m: self._email_masked(), out)
        self._mask_cache[s] = out
        return out

    def _next(self) -> int:
        self._c += 1
        return self._c

    def _digits(self, n: int) -> str:
        return "".join(str(self.r.randint(0, 9)) for _ in range(n))

    def _fio_plain(self) -> str:
        if self.r.random() < 0.5:
            return f"{self.r.choice(_FAM)} {self.r.choice(_NAME_M)} {self.r.choice(_PATR_M)}"
        return f"{self.r.choice(_FAM)}а {self.r.choice(_NAME_F)} {self.r.choice(_PATR_F)}"

    def _fio_initials(self) -> str:
        return f"{self.r.choice(_FAM)} {self.r.choice(_NAME_M)[0]}.{self.r.choice(_PATR_M)[0]}."

    def _email_masked(self) -> str:
        return f"user{self._next():05d}@{self.r.choice(_DOMAINS)}"
