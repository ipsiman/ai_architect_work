# Проект «Умный помощник командировок»

Мультиагентные системы + RAG.

## Структура проекта

| Файл                                     | Назначение |
|------------------------------------------|-----------|
| [`01_architecture.md`](01_architecture.md) | Роли агентов, паттерн, схемы взаимодействия (Mermaid), использование RAG |
| [`02_rag_flow.md`](02_rag_flow.md)       | Пайплайн RAG: чанкинг, эмбеддинг, Vector DB, реранжинг, метрики |
| [`trip_assistant.py`](trip_assistant.py) | Рабочий прототип: Менеджер → Поисковик + мини-RAG (YandexGPT / MOCK) |

## Как запустить прототип

Прототип — единый скрипт [`trip_assistant.py`](trip_assistant.py).

**Установить зависимости:**
```bash
pip install "langgraph>=0.2,<1.0" "langchain-core>=0.3,<1.0" "langchain-openai>=0.2,<1.0" "langchain-text-splitters>=0.3,<1.0"
```

**Локально (MOCK-режим, ключи не нужны):**
```bash
python trip_assistant.py
```

**Локально с реальным YandexGPT:**
```bash
YANDEX_API_KEY=<ключ> YANDEX_FOLDER_ID=<каталог> python trip_assistant.py
```

**В Colab:** Перейдите по ссылке - [`trip_assistant.ipynb`](https://colab.research.google.com/drive/1hyFeyhAA-3GMqSb9nY8KBRrO_UUHmyYE?usp=sharing). Задайте переменные `YANDEX_API_KEY` / `YANDEX_FOLDER_ID` и запускайте.
Без ключей скрипт автоматически работает в `MOCK_MODE`: обмен сообщениями между агентами
демонстрируется на заглушках LLM.

В выводе видно: шаги агентов, обмен сообщениями через общий state и итоговый ответ Менеджера.
