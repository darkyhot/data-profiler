"""Классификатор чувствительности колонок по ИМЕНИ (+ немного по типу).

Задача: отделить бизнес-категории (тип задачи, статус — их РЕАЛЬНЫЕ значения
надо сохранить) от персональных/финансовых данных (ФИО, ИНН, счёт, сумма —
их реальные значения НЕ должны покидать контур даже в профиле; синтезируются
полностью фейковыми, «не похожими на настоящие»).

Возвращаем не только флаг, но и КАТЕГОРИЮ — чтобы фейкер/LLM знали, что
именно подделывать (инн, фио, телефон, email, деньги, …).
"""

from __future__ import annotations

import re

# (категория, regex по имени колонки). Первое совпадение выигрывает.
_RULES: list[tuple[str, re.Pattern]] = [
    ("inn",     re.compile(r"(^|_)(inn|kpp|ogrn|ogrnip|okpo)($|_)", re.I)),
    ("snils",   re.compile(r"(^|_)(snils|inn_fl)($|_)", re.I)),
    ("passport",re.compile(r"(pasport|passport|pasp|doc_ser|doc_num|seria|документ)", re.I)),
    ("fio",     re.compile(r"(^|_)(fio|fam|famil|surname|lastname|firstname|name_last|"
                           r"name_first|otchestvo|patronymic|middlename|фио)($|_)", re.I)),
    ("person",  re.compile(r"(client_name|customer_name|person|owner_name|holder|"
                           r"full_name|display_name)", re.I)),
    ("phone",   re.compile(r"(^|_)(phone|tel|mobile|telefon|msisdn)($|_)", re.I)),
    ("email",   re.compile(r"(^|_)(email|e_mail|mail)($|_)", re.I)),
    ("account", re.compile(r"(^|_)(account|acc_no|schet|schyot|card|karta|pan|iban|bik|"
                           r"corr_acc|kor_schet)($|_)", re.I)),
    ("address", re.compile(r"(^|_)(address|adres|street|ulica|house|dom|kvartira|"
                           r"index|postcode|zip)($|_)", re.I)),
    ("birth",   re.compile(r"(birth|rojd|dob|born|дата_рожд)", re.I)),
    ("geo",     re.compile(r"(^|_)(lat|lon|latitude|longitude|geo|coord)($|_)", re.I)),
    ("money",   re.compile(r"(^|_)(amount|amt|sum|summa|balance|balans|price|cost|"
                           r"salary|zarplata|oklad|debt|dolg|payment|oplata|revenue|"
                           r"turnover|oborot|limit)($|_)", re.I)),
]


def classify(column_name: str) -> tuple[bool, str]:
    """→ (is_sensitive, category). category='' если не чувствительна."""
    name = column_name or ""
    for category, rx in _RULES:
        if rx.search(name):
            return True, category
    return False, ""
