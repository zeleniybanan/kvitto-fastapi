# Квитто: API платежей

Тестовое Junior Python / FastAPI. Python **3.11+**, FastAPI, Pydantic v2,
SQLAlchemy и SQLite. Все суммы - целые копейки. Реального списания денег нет.

## Запуск

Команды выполняются из корня проекта. Нужен Python 3.11 или новее;
проверьте `python --version` (на Windows `py -0p`). Python 3.9 не подходит.

Windows PowerShell, с установленным Python 3.12:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

macOS / Linux:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m uvicorn app.main:app --reload
```

Для Python 3.11 замените `3.12` на `3.11`. Активация окружения на Windows не нужна.
Для запуска без инструментов разработки достаточно `requirements.txt`.

Swagger UI: [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).
При старте автоматически применяются миграции Alembic и добавляются отсутствующие
тарифы. Файл `payments.db` сохраняется между запусками. Запускайте **один процесс**
сервера: одновременный запуск миграций несколькими процессами здесь не предусмотрен.
В `.env` приложение само не заглядывает: переменные задаются в оболочке.

## Проверки

Windows:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
```

macOS / Linux после активации окружения:

```bash
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
```

Тесты создают временные базы. Покрыты суммы всех тарифов, промокод в разных регистрах,
3/6/12 месяцев, все 16 переходов статусов, ошибки, фильтры, HMAC, конкурентные запросы,
перезапуск и миграции. Результаты проверки: [VERIFICATION.md](VERIFICATION.md).

## API

| Запрос | Результат |
| --- | --- |
| `GET /tariffs` | `200`, список `{id,title,price}` |
| `POST /payments` | `201`, платёж; повтор ключа - `200`, тот же платёж |
| `GET /payments/{id}` | `200`, платёж; отсутствует - `404` |
| `POST /webhooks/bank` | `200 {"result":"ok"}`; нет платежа - `404`; запрещённый переход - `409 {"error":"invalid_transition"}` |
| `GET /payments` | `200`, список; фильтры `email`, `status`, `limit` (1-100), `offset` |

Тарифы: `basic` - 990000, `standard` - 1990000, `premium` - 2990000 копеек.
`KVITTO10` даёт скидку 10%, регистр не важен; другой код - `422`.
`method`: `card`, `sbp`, `installment`. Для рассрочки `installment_months` обязателен
и равен 3, 6 или 12; для остальных способов это поле должно отсутствовать или быть `null`.
Ошибки валидации имеют стандартную структуру FastAPI `{"detail":[...]}` и код `422`.

Новый платёж - `pending`. Допустимы только `pending -> succeeded`,
`pending -> failed`, `succeeded -> refunded`. Повтор того же статуса тоже даёт `409`.
В ответе: `id`, `status`, `tariff_id`, `amount`, `discount`, `method`,
`installment_months`, `schedule`, `email`, `created_at`.
Время сохраняется в UTC; SQLite возвращает его без суффикса часового пояса.

`Idempotency-Key` - необязательный непустой заголовок до 255 символов.
Один ключ означает один платёж, даже при другом **валидном** теле повторного запроса:
возвращается сохранённый платёж в текущем состоянии. Так задано в ТЗ.
Без ключа каждый запрос создаёт новый платёж. Невалидное тело даёт `422` до обработки ключа.

## curl: macOS / Linux / Git Bash

```bash
curl http://127.0.0.1:8000/tariffs

curl -i http://127.0.0.1:8000/payments \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-order-1' \
  -d '{"tariff_id":"standard","email":"student@example.org","method":"installment","installment_months":3,"promo_code":"kvitto10"}'

# Скопируйте id из ответа; повтор предыдущего запроса вернёт 200 и тот же id.
ID='ВСТАВЬТЕ_ID'
curl "http://127.0.0.1:8000/payments/$ID"
curl 'http://127.0.0.1:8000/payments?email=student%40example.org&status=pending'

curl http://127.0.0.1:8000/webhooks/bank \
  -H 'Content-Type: application/json' \
  -d "{\"payment_id\":\"$ID\",\"status\":\"succeeded\"}"
```

Для `standard` с промокодом ожидается `amount=1791000`, `discount=199000`,
`schedule=[597000,597000,597000]`. Без промокода график на 3 месяца:
`[663334,663333,663333]`.

PowerShell: используйте `curl.exe`. Чтобы избежать различий экранирования между
Windows PowerShell 5 и PowerShell 7, передавайте JSON через файл:

```powershell
Set-Content -Encoding ascii payment.json '{"tariff_id":"standard","email":"student@example.org","method":"card"}'
curl.exe -i http://127.0.0.1:8000/payments -H 'Content-Type: application/json' -H 'Idempotency-Key: windows-demo-1' --data-binary '@payment.json'
```

## Подпись вебхука

Если `WEBHOOK_SECRET` отсутствует или пуст, подпись не требуется: основной сценарий ТЗ
работает без настройки. Если секрет задан **до запуска сервера**, каждый вебхук обязан
содержать `X-Signature`: HMAC-SHA256 от точных байтов тела, hex в нижнем регистре.
Отсутствующая, неверная подпись или изменённый пробел в теле дают `401`; БД не меняется.

В терминале сервера перед запуском:

```powershell
$env:WEBHOOK_SECRET = 'local-demo-secret'
# Затем запустите uvicorn как выше.
```

В другом PowerShell-терминале, после создания платежа:

```powershell
$env:WEBHOOK_SECRET = 'local-demo-secret'
$paymentId = 'ВСТАВЬТЕ_ID'
$signature = .\.venv\Scripts\python.exe scripts/sign_webhook.py $paymentId succeeded
curl.exe http://127.0.0.1:8000/webhooks/bank -H 'Content-Type: application/json' -H "X-Signature: $signature" --data-binary '@webhook.json'
```

Bash после активации окружения:

```bash
export WEBHOOK_SECRET='local-demo-secret'  # также в терминале сервера до его запуска
SIGNATURE=$(python scripts/sign_webhook.py "$ID" succeeded)
curl http://127.0.0.1:8000/webhooks/bank \
  -H 'Content-Type: application/json' -H "X-Signature: $SIGNATURE" \
  --data-binary @webhook.json
```

Скрипт сохраняет и подписывает одни и те же байты. Секрет не передаётся в запросе.

## Docker и миграции

```bash
docker compose up --build
docker compose down
```

Данные в named volume `payments` сохраняются после `down`. Порт доступен только
локально. `WEBHOOK_SECRET` передаётся из окружения; перезапустите контейнер после изменения.
Статус проверки Docker указан в [VERIFICATION.md](VERIFICATION.md).

Alembic можно вызвать вручную (в Windows вместо `python` используйте путь к Python из `.venv`):

```bash
python -m alembic upgrade head
python -m alembic current
python -m alembic check
```

`DATABASE_URL` по умолчанию `sqlite:///./payments.db`; относительный путь считается от
рабочего каталога. Для другой SQLite-базы задайте переменную до запуска приложения или Alembic.
Откат `python -m alembic downgrade base` удаляет таблицы и данные - только для пустой тестовой базы.
Если используете старую базу из исходного проекта без Alembic, сначала сохраните её копию;
этот проект рассчитан на новую базу, автоматического переноса старой схемы нет.

## Структура и решения

- `app/main.py`: запуск, миграции, тарифы, сессии БД и пять эндпоинтов.
- `app/models.py`: две таблицы, уникальный индекс ключа платежа.
- `app/schemas.py`: валидация и ответы; `app/logic.py`: деньги и таблица переходов.
- `migrations/`: начальная схема; `tests/`: проверки поведения через HTTP и БД.
- `.github/workflows/checks.yml`: ruff и pytest на push/PR, Python 3.11 и 3.12.
- `AGENTS.md`: правила для ассистента; `AI_LOG.md`: фактическое использование ИИ.

Синхронные обработчики подходят для синхронной SQLAlchemy: FastAPI выполняет их в пуле потоков.
Проверка подписи асинхронная, потому что читает тело запроса. Для рассрочки используется
`divmod`: частное - базовый платёж, остаток - число первых платежей с дополнительной копейкой.
Дубли блокирует уникальный индекс БД, а не только предварительный SELECT.
Переход статуса - условный UPDATE по прежнему значению; проигравший гонку получает `409`.

Область решения: локальное тестовое на SQLite. PostgreSQL, несколько процессов сервера,
аутентификация пользователей, реальный банк и деплой здесь не проверялись и не заявляются.

## Сдача

Создайте репозиторий GitHub и загрузите содержимое этой папки в его корень, включая
`.github`, `.gitignore`, `AGENTS.md` и `AI_LOG.md`. Не загружайте `.venv`, базы и секреты.
Дождитесь проверки Actions. Для приватного репозитория предоставьте проверяющему доступ.
Отправьте ссылку в исходный чат с работодателем. Архив сам по себе не заменяет репозиторий.
