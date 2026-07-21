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
    # логины/авторы/исполнители — часто содержат ФИО+email внутри значения → freeform
    ("freeform", re.compile(r"(^|_)(login|author|autor|executor|ispolnitel|ispoln|"
                            r"manager|employee|sotrudnik|user_name|username|created_by|"
                            r"updated_by|modified_by|responsible|otvetstv)($|_)", re.I)),
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


# «Предметные» токены: имя-НАИМЕНОВАНИЕ объекта, а не человека. Напр. tb_full_name
# (территориальный банк), org_name, product_name — это справочные категории, их
# НЕ маскируем. Гасят ложное срабатывание правил fio/person (но НЕ inn/account/…,
# чтобы bank_account остался чувствительным).
_REFERENCE_RE = re.compile(
    r"(^|_)(tb|bank|org|orgs|company|firm|branch|filial|division|dept|department|"
    r"product|service|terr|territor|region|okrug|city|gorod|office|unit|otdel|"
    r"podrazdel|holding|group|osb|gosb|system|channel|kanal|type|status|category|"
    r"kategor|classification|klass)(_|$)", re.I,
)
_NAME_CATEGORIES = {"fio", "person"}


def classify(column_name: str, *, force_sensitive: dict[str, str] | None = None,
             force_non_sensitive: set[str] | None = None) -> tuple[bool, str]:
    """→ (is_sensitive, category). category='' если не чувствительна.

    Оверрайды пользователя (по имени колонки в нижнем регистре) имеют приоритет
    над эвристикой: force_non_sensitive «размаскирует», force_sensitive задаёт
    категорию фейка принудительно.
    """
    name = column_name or ""
    low = name.lower()
    fns = force_non_sensitive or set()
    fs = force_sensitive or {}
    if low in fns:
        return False, ""
    if low in fs:
        return True, fs[low]
    for category, rx in _RULES:
        if rx.search(name):
            # наименование объекта (банк/орг/продукт …), а не ФИО → не маскируем
            if category in _NAME_CATEGORIES and _REFERENCE_RE.search(name):
                return False, ""
            return True, category
    return False, ""


# ── детект чувствительности по СОДЕРЖИМОМУ значений ───────────────────────────
# Ловит PII, которую не видно по имени колонки (напр. author_login со встроенными
# ФИО и email). Возвращает kind: "email"/"fio" если ВСЁ значение — это email/ФИО,
# иначе "freeform" (PII встроена в структуру → маскируем с сохранением формата).
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.U)
_FIO3_RE = re.compile(r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+", re.U)


def detect_by_values(values: list[str], *, threshold: float = 0.3) -> tuple[bool, str]:
    vals = [v for v in (str(x).strip() for x in values) if v]
    if len(vals) < 5:                     # мало данных для надёжного вывода
        return False, ""
    n = len(vals)
    email_hits = sum(1 for v in vals if _EMAIL_RE.search(v))
    fio_hits = sum(1 for v in vals if _FIO3_RE.search(v))
    if email_hits / n < threshold and fio_hits / n < threshold:
        return False, ""
    exact_email = sum(1 for v in vals if _EMAIL_RE.fullmatch(v))
    exact_fio = sum(1 for v in vals if _FIO3_RE.fullmatch(v))
    if exact_email / n >= 0.6:
        return True, "email"
    if exact_fio / n >= 0.6:
        return True, "fio"
    return True, "freeform"              # PII встроена в структуру
