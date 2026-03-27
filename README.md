# Payment Processing Service

Асинхронный микросервис для обработки платежей. Принимает запросы через REST API, обрабатывает через эмуляцию платёжного шлюза и уведомляет клиента по webhook.

## Стек

- **FastAPI** + Pydantic v2 — API
- **SQLAlchemy 2.0** (async) + **PostgreSQL** — хранение данных
- **RabbitMQ** + **FastStream** — очередь сообщений
- **Alembic** — миграции
- **Docker Compose** — развёртывание

## Архитектура

```
POST /api/v1/payments
        │
        ▼
  [API] Создаёт Payment + OutboxEvent в одной транзакции
        │
        ▼
  [Outbox Publisher] Фоновый loop читает OutboxEvent.published=False
        │           публикует в RabbitMQ → помечает published=True
        ▼
  [RabbitMQ] payments.new queue
        │           (при ошибках после 3 попыток → payments.dlq)
        ▼
  [Worker/Consumer] Обрабатывает платёж (2-5 сек, 90% успех)
        │           Обновляет статус в БД
        ▼
  [Webhook] POST на webhook_url с результатом (retry 1→2→4 сек)
```

## Запуск

```bash
# Клонируем и запускаем
docker-compose up --build
```

Сервисы:
- API: http://localhost:8000
- Swagger UI: http://localhost:8000/docs
- RabbitMQ Management: http://localhost:15672 (guest/guest)

## Примеры запросов

### Создание платежа

```bash
curl -X POST http://localhost:8000/api/v1/payments \
  -H "Content-Type: application/json" \
  -H "X-API-Key: super-secret-api-key" \
  -H "Idempotency-Key: unique-key-123" \
  -d '{
    "amount": 1500.00,
    "currency": "RUB",
    "description": "Оплата заказа #42",
    "metadata": {"order_id": 42, "user_id": 100},
    "webhook_url": "https://webhook.site/your-uuid"
  }'
```

Ответ `202 Accepted`:
```json
{
  "payment_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "pending",
  "created_at": "2024-01-15T10:30:00Z"
}
```

### Получение информации о платеже

```bash
curl http://localhost:8000/api/v1/payments/550e8400-e29b-41d4-a716-446655440000 \
  -H "X-API-Key: super-secret-api-key"
```

Ответ:
```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "amount": "1500.00",
  "currency": "RUB",
  "description": "Оплата заказа #42",
  "metadata": {"order_id": 42, "user_id": 100},
  "status": "succeeded",
  "idempotency_key": "unique-key-123",
  "webhook_url": "https://webhook.site/your-uuid",
  "created_at": "2024-01-15T10:30:00Z",
  "processed_at": "2024-01-15T10:30:04Z"
}
```

### Идемпотентность

Повторный запрос с тем же `Idempotency-Key` вернёт данные уже существующего платежа:

```bash
# Этот запрос вернёт тот же payment_id что и первый
curl -X POST http://localhost:8000/api/v1/payments \
  -H "X-API-Key: super-secret-api-key" \
  -H "Idempotency-Key: unique-key-123" \
  -d '{"amount": 9999, "currency": "USD"}'
```

## Конфигурация

Настройки через `.env` файл (см. `.env.example`):

| Переменная | Описание | Пример |
|---|---|---|
| `DATABASE_URL` | PostgreSQL DSN | `postgresql+asyncpg://...` |
| `RABBITMQ_URL` | RabbitMQ AMQP URL | `amqp://guest:guest@...` |
| `API_KEY` | Статический ключ для X-API-Key | `super-secret-api-key` |

## Гарантии доставки

**Outbox Pattern**: платёж и событие создаются в одной транзакции. Даже если приложение упадёт после записи в БД — событие не потеряется и будет опубликовано при следующем старте.

**Retry**: при ошибке обработки FastStream повторяет попытку 3 раза. Если все 3 провалились — сообщение уходит в `payments.dlq`.

**Webhook Retry**: при ошибке отправки webhook — 3 попытки с задержками 1→2→4 секунды.
