# Асинхронный сервис платежей

Микросервис принимает платёж, сохраняет его вместе с событием в PostgreSQL,
обрабатывает через RabbitMQ и отправляет результат в webhook. Платёжный шлюз
эмулируется: задержка 2–5 секунд, 90% `succeeded`, 10% `failed`.

Стек: Python 3.12+, FastAPI / Pydantic v2, SQLAlchemy 2.0 async / asyncpg,
PostgreSQL, RabbitMQ / FastStream, Alembic, Docker Compose v2.
Версии Python-зависимостей зафиксированы в `requirements*.lock`.

## Запуск в Docker

Из каталога проекта в PowerShell:

```powershell
Copy-Item .env.example .env
docker compose up --build -d
docker compose ps
docker compose logs -f consumer
```

Перед использованием вне локальной демонстрации замените ключ и пароли в `.env`.
Для паролей в этом Compose используйте буквы и цифры: они подставляются в URL.
Повторный запуск не требует копировать `.env` заново.

Compose запускает PostgreSQL, RabbitMQ, разовую миграцию, API и один consumer.
API и consumer стартуют после успешной миграции; consumer также ждёт RabbitMQ.
Данные БД и брокера хранятся в именованных volumes. Порты опубликованы на localhost.

- API: [http://localhost:8000](http://localhost:8000).
- RabbitMQ Management: [http://localhost:15672](http://localhost:15672),
  пользователь `payments`, пароль `RABBITMQ_PASSWORD` из `.env`.
- Остановка с сохранением данных: `docker compose down`.

## Попробовать платёж и повторные попытки

В другом терминале запустите демонстрационный получатель webhook на хосте:

```powershell
python tools/webhook_receiver.py --fail-first 2
```

Он вернёт `503` на первые две доставки каждого события и `204` на третью.
Это локальный стенд; для обычного успеха уберите `--fail-first 2`.

Создайте платёж из PowerShell:

```powershell
$apiKey = "local-development-key-change-me" # либо ваш API_KEY из .env
$headers = @{ "X-API-Key" = $apiKey; "Idempotency-Key" = "order-1001" }
$body = @{
    amount = "1499.90"
    currency = "RUB"
    description = "Order 1001"
    metadata = @{ order_id = "1001" }
    webhook_url = "http://host.docker.internal:9000/webhook"
} | ConvertTo-Json -Depth 10
$payment = Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/v1/payments" `
    -Headers $headers -ContentType "application/json" -Body $body
$payment
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/payments/$($payment.payment_id)" `
    -Headers @{ "X-API-Key" = $apiKey }
```

Создание возвращает `202 Accepted`:

```json
{
  "payment_id": "3ca42c55-71d3-4dd2-a0de-3e2154d64f56",
  "status": "pending",
  "created_at": "2026-10-30T09:00:00Z"
}
```

`GET /api/v1/payments/{payment_id}` возвращает сумму строкой с точностью до копеек,
валюту, описание, `metadata`, `idempotency_key`, URL webhook, статус, даты создания
и обработки, `webhook_delivered_at`, `attempts_completed` и `last_error`.
Повторите GET через несколько секунд, чтобы увидеть окончательный результат.

Повтор POST с тем же ключом и теми же данными возвращает тот же ID и текущий статус,
не создавая платёж или событие заново. Изменённое тело с занятым ключом даёт `409`.
Порядок ключей JSON и эквивалентные суммы `1` / `1.00` не влияют на сравнение.

| Ситуация | HTTP |
|---|---|
| Новый платёж или идемпотентный повтор | 202 |
| Неизвестный платёж | 404 |
| Нет API-ключа или он неверен | 401 |
| Занятый Idempotency-Key с другим телом | 409 |
| Нет Idempotency-Key, неверная сумма / валюта / URL / UUID | 422 |

Все маршруты защищены `X-API-Key`, включая `GET /health` и `GET /openapi.json`.
`/health` проверяет API и доступность PostgreSQL; состояние consumer смотрите
в `docker compose ps` и логах. Публичные Swagger / ReDoc отключены.
`Idempotency-Key` обязателен для POST, непустой, максимум 255 символов.
Сумма положительная, максимум 16 целых и 2 дробных разряда; валюты RUB, USD, EUR.

## Как устроены гарантии доставки

```text
POST -> PostgreSQL [payments + outbox, одна транзакция]
                        |
              OutboxRelay в consumer
                        | persistent + publisher confirm + mandatory
                        v
                RabbitMQ payments.new
                        |
              один обработчик платежа
                        |
              шлюз -> COMMIT результата -> webhook
                        |
        ошибка -> COMMIT счётчика и следующего outbox-события
                        |
             1 сек -> 2 сек -> payments.dlx -> payments.dead
```

1. **Outbox.** Создание платежа и первого события атомарно. Relay читает готовые
   записи через `FOR UPDATE SKIP LOCKED`. `published_at` записывается после
   подтверждённой RabbitMQ публикации. При сбое записи БД или публикации событие
   остаётся для следующего прохода. RabbitMQ недоступен — API продолжает принимать
   платежи, пока доступна БД; события ждут восстановления consumer / брокера.
2. **Идемпотентность.** Уникальный индекс БД защищает даже конкурентные POST.
   Блокировка строки платежа и `attempts_completed` отсекают повторные сообщения.
   Терминальный результат шлюза сохраняется до webhook, поэтому ошибка доставки
   не запускает платёж заново.
3. **Retry.** Всего **3 попытки**, включая первую; между ними задержки 1 и 2 секунды
   плюс время polling relay. Формула: `RETRY_BASE_SECONDS * 2^(attempt-1)`.
   Отложенное время хранится в `outbox.available_at`, а не в памяти и не в TTL-очереди.
   Consumer подтверждает исходное сообщение только после завершения обработчика;
   ошибка фиксируется вместе со следующим событием в одной транзакции.
4. **DLQ.** После третьей ошибки создаётся outbox-событие для durable exchange
   `payments.dlx` с маршрутом `payments.dead`; оно содержит ID, попытку и тип ошибки.
   Durable очередь `payments.dead` доступна в RabbitMQ Management. Некорректные
   сообщения отклоняются без requeue и попадают туда через DLX исходной очереди.
5. **Инфраструктурный сбой.** Если БД не позволяет сохранить результат или retry,
   обработчик выбрасывает исключение; FastStream делает NACK с requeue. Такие
   доставки не расходуют прикладной лимит, пока БД не сможет сохранить попытку.

Отклонение шлюзом (`failed`, 10%) — завершённый финансовый результат, о котором
тоже отправляется webhook. Оно само по себе не вызывает retry. Ошибка webhook
не меняет уже сохранённый финансовый статус. Если все попытки обращения к шлюзу
завершились инфраструктурными ошибками, платёж остаётся `pending` с `last_error`
и сообщением в DLQ для разбора.

Доставка событий и webhook — **at least once**. Сбой после публикации, но до коммита
relay может привести к дублю. Сбой после принятия webhook получателем, но до коммита
consumer тоже может повторить уведомление. Получатель должен дедуплицировать
стабильный `event_id`, также передаваемый в заголовке `Idempotency-Key`.
Для реального внешнего шлюза потребуется его собственный idempotency key;
здесь шлюз является эмуляцией без внешнего списания денег.

Пример webhook:

```json
{
  "event_id": "payment:3ca42c55-71d3-4dd2-a0de-3e2154d64f56:result",
  "event": "payment.processed",
  "payment_id": "3ca42c55-71d3-4dd2-a0de-3e2154d64f56",
  "status": "succeeded",
  "amount": "1499.90",
  "currency": "RUB",
  "description": "Order 1001",
  "metadata": {"order_id": "1001"},
  "created_at": "2026-10-30T09:00:00+00:00",
  "processed_at": "2026-10-30T09:00:03+00:00"
}
```

Успех webhook — любой 2xx. Тайм-аут по умолчанию 10 секунд; редиректы не выполняются.
URL задаёт клиент с API-ключом; локальная демонстрация допускает внутренние адреса.
Подтверждённые outbox-записи сохраняются для аудита; их фоновая очистка не реализована.
Compose рассчитан на одну локальную инсталляцию, без репликации БД и брокера.

## Проверки

Быстрые тесты не требуют PostgreSQL или RabbitMQ:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.lock
.venv\Scripts\python -m pip install --no-deps -e .
.venv\Scripts\python -m pytest -q
.venv\Scripts\ruff check .
.venv\Scripts\ruff format --check .
```

Проверяются API, авторизация, валидация сумм, идемпотентность, атомарный rollback,
оба результата шлюза, дубли, тайм-ауты webhook, задержки retry, исчерпание попыток,
сбой relay и обработка некорректных сообщений через FastStream TestRabbitBroker.
SQLite используется только в быстрых тестах; он не проверяет блокировки PostgreSQL.

Полные интеграционные тесты поверх поднятого Compose:

```powershell
docker compose --profile test build tests
docker compose --profile test run --rm --use-aliases tests
```

Три интеграционных сценария проверяют конкурентные POST на PostgreSQL, реальную
доставку webhook, три ошибки и публикацию в DLX, а также `SKIP LOCKED` в отдельной
временной схеме. Запускайте на тестовом Compose: сценарии создают тестовые платежи.
Внутри tests-контейнера автоматически запускается HTTP-получатель webhook.

В среде подготовки выполнены быстрые тесты и Ruff, проверена генерация SQL миграции
для PostgreSQL. Docker здесь отсутствует; сборка контейнеров и три интеграционных
сценария не выполнялись. Точные результаты: `VALIDATION.md`.

## Запуск Python-процессов без Docker

Для локальной разработки с уже доступными PostgreSQL и RabbitMQ:

```powershell
# Установите зависимости командами выше, заполните .env.
.venv\Scripts\alembic upgrade head
.venv\Scripts\uvicorn app.api:create_app --factory --host 127.0.0.1 --port 8000
# В другом терминале:
.venv\Scripts\python -m app.worker
```

Параметры `DATABASE_URL` и `RABBITMQ_URL` в `.env.example` используют localhost;
Compose заменяет их именами контейнеров. Дополнительно можно настроить
`OUTBOX_POLL_SECONDS`, `RETRY_BASE_SECONDS`, `WEBHOOK_TIMEOUT_SECONDS`,
`GATEWAY_MIN_SECONDS` и `GATEWAY_MAX_SECONDS` (последние два для локального worker).

## Структура

| Файл / каталог | Назначение |
|---|---|
| `app/api.py`, `schemas.py`, `payments.py` | API, валидация, атомарное создание |
| `app/models.py`, `database.py` | Модели SQLAlchemy и async-сессии |
| `app/processing.py`, `gateway.py` | Обработка платежа, webhook, durable retry |
| `app/outbox.py` | Публикация с подтверждением и блокировками |
| `app/messaging.py`, `worker.py` | Топология RabbitMQ и один consumer |
| `alembic/versions/0001_payments_outbox.py` | Миграция и обратный откат |
| `tests/` | Быстрые и интеграционные тесты |
| `tools/webhook_receiver.py` | Локальный демонстрационный webhook |

Документация библиотек:
[SQLAlchemy AsyncIO](https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html),
[FastStream RabbitBroker](https://faststream.ag2.ai/latest/api/faststream/rabbit/RabbitBroker/),
[RabbitMQ confirms](https://www.rabbitmq.com/docs/confirms).
