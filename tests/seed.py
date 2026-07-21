"""Наполнение тест-контейнера правдоподобными данными для проверки профайлера.

Моделируем прод-ситуацию:
- sn_uzp.task — реальные данные, БЕЗ комментариев (как в проде).
- sn_view.task — та же таблица с КОММЕНТАРИЯМИ (redirect-источник описаний).
- sn_t_uzp.task_log — свои комментарии (redirect на себя).
Корреляция: task_questionary зависит от task_subtype (не должны перемешаться).
Чувствительные: client_inn, client_fio, amount.
"""
import random

import pandas as pd
from sqlalchemy import create_engine, text

URL = "postgresql+psycopg2://user:pass@localhost:55432/db"
r = random.Random(1)

TYPES = {
    "Отток": ["Отток-звонок", "Отток-письмо", "Отток-визит"],
    "Привлечение": ["Привлечение-холодный", "Привлечение-тёплый"],
    "Удержание": ["Удержание-скидка", "Удержание-подарок", "Удержание-звонок"],
}
# опросник СВОЙ для каждого подтипа
QUEST = {
    "Отток-звонок": ["Причина ухода: цена", "Причина ухода: сервис", "Причина ухода: конкурент"],
    "Отток-письмо": ["Открыл письмо: да", "Открыл письмо: нет"],
    "Отток-визит": ["Визит состоялся", "Визит отменён клиентом"],
    "Привлечение-холодный": ["Интерес: низкий", "Интерес: средний", "Интерес: высокий"],
    "Привлечение-тёплый": ["Готов к сделке: да", "Готов к сделке: думает"],
    "Удержание-скидка": ["Скидка принята", "Скидка отклонена"],
    "Удержание-подарок": ["Подарок вручён", "Подарок не актуален"],
    "Удержание-звонок": ["Дозвон: успешно", "Дозвон: нет ответа", "Дозвон: перезвонить"],
}
FIO = ["Смирнов Олег Иванович", "Кузнецова Анна Петровна", "Попов Игорь Сергеевич",
       "Волкова Мария Алексеевна", "Соколов Дмитрий Николаевич"]

rows = []
for i in range(1, 4001):
    t = r.choice(list(TYPES))
    st = r.choice(TYPES[t])
    q = r.choice(QUEST[st]) if r.random() > 0.1 else None   # ~10% null
    rows.append({
        "task_id": i,
        "task_type": t,
        "task_subtype": st,
        "task_questionary": q,
        "client_inn": "".join(str(r.randint(0, 9)) for _ in range(10)),
        "client_fio": r.choice(FIO),
        "amount": round(r.uniform(100, 500000), 2) if r.random() > 0.2 else None,
        "status": r.choice(["new", "in_progress", "done", "cancelled"]),
        "created_dttm": pd.Timestamp("2024-01-01") + pd.Timedelta(days=r.randint(0, 500)),
    })
df = pd.DataFrame(rows)

eng = create_engine(URL)
with eng.begin() as c:
    for s in ("sn_uzp", "sn_view", "sn_t_uzp"):
        c.execute(text(f"DROP SCHEMA IF EXISTS {s} CASCADE"))
        c.execute(text(f"CREATE SCHEMA {s}"))

df.to_sql("task", eng, schema="sn_uzp", if_exists="replace", index=False)

# sn_view.task — пустой каркас той же таблицы, только ради КОММЕНТАРИЕВ (redirect)
with eng.begin() as c:
    c.execute(text("CREATE TABLE sn_view.task (LIKE sn_uzp.task)"))
    c.execute(text("COMMENT ON TABLE sn_view.task IS 'Задачи по работе с клиентами'"))
    comments = {
        "task_id": "Идентификатор задачи",
        "task_type": "Тип задачи (бизнес-категория)",
        "task_subtype": "Подтип задачи",
        "task_questionary": "Результат опросника по подтипу",
        "client_inn": "ИНН клиента",
        "client_fio": "ФИО клиента",
        "amount": "Сумма по задаче, руб.",
        "status": "Статус задачи",
        "created_dttm": "Момент создания задачи",
    }
    for col, com in comments.items():
        c.execute(text(f"COMMENT ON COLUMN sn_view.task.{col} IS :c"), {"c": com})

# sn_t_uzp.task_log — свои комментарии
df[["task_id", "status", "created_dttm"]].to_sql("task_log", eng, schema="sn_t_uzp",
                                                  if_exists="replace", index=False)
with eng.begin() as c:
    c.execute(text("COMMENT ON COLUMN sn_t_uzp.task_log.status IS 'Статус в журнале'"))

print("seeded:", len(df), "rows into sn_uzp.task; comments in sn_view.task; sn_t_uzp.task_log")
