# Результаты проверки

Дата: 30 октября 2026 года. Среда: Windows, Python 3.12.10.

## Полная проверка в GitHub Actions

[Успешный прогон](https://github.com/ca1mik/async-payment-service/actions/runs/41035878798)
для коммита `62372e3ddd005ec65aad374f27ba9d4d0e505fd7`:

- `unit`: success; установка зависимостей, сборка пакета, Ruff и быстрые тесты.
- `compose-integration`: success; Docker-образы собраны, PostgreSQL/RabbitMQ/API/consumer
  запущены, Alembic применил миграцию, **38 passed in 14.32s**, пропусков нет.
- Проверены конкурентные POST, реальная доставка webhook, три ошибки отправки,
  публикация в DLX и `SKIP LOCKED` на PostgreSQL.

Полный CI автоматически запускается при push и pull request.

## Локальная проверка на Windows

| Проверка | Результат |
|---|---|
| `pytest -q` | 35 passed, 3 skipped |
| `ruff check .` | Успешно |
| `ruff format --check .` | Успешно |
| `pip check` | No broken requirements found |
| Сборка wheel и `pip install --no-deps .` | Успешно |
| `alembic upgrade head --sql` с PostgreSQL URL | Успешная генерация DDL |
| `alembic downgrade 0001:base --sql` | Успешная генерация обратного DDL |
| Разбор `compose.yaml` и разрешение YAML anchors | Успешно |

Пропущенные тесты требуют работающих PostgreSQL и RabbitMQ:

1. Конкурентные POST создают один платёж и одно исходное событие; реальный webhook.
2. Три неуспешные доставки webhook приводят к событию в `payments.dlx`.
3. Уникальность и `FOR UPDATE SKIP LOCKED` на настоящем PostgreSQL.

Docker отсутствует в локальной среде подготовки. Сборка образов, применение миграции
к настоящему PostgreSQL и запуск полного стека проверены удалённо в CI, указанном выше.
Разбор YAML не заменяет `docker compose config` или запуск контейнеров.
Быстрые тесты используют SQLite и FastStream TestRabbitBroker; они не подтверждают
сетевые гарантии RabbitMQ или поведение блокировок PostgreSQL.

Для полной проверки из каталога проекта:

```powershell
Copy-Item .env.example .env # только при первом запуске
docker compose up --build -d
docker compose --profile test build tests
docker compose --profile test run --rm --use-aliases tests
```

При локальном запуске pytest в среде подготовки системный каталог Temp оказался
недоступен. Тесты выполнены с `--basetemp`, указывающим на отдельный рабочий каталог.
Это не меняет код тестов; в обычном окружении дополнительный параметр не требуется.
