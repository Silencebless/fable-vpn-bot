# Fable | VPN — что сделать до запуска

Актуально на ноябрь 2026: Remnawave **3.4.x**, aiogram **3.3x**, ЮKassa API **v3**.
Ссылки на официальную документацию — в каждом разделе: именно она источник правды, если интерфейс изменится.

Порядок: **Telegram → панель Remnawave → ЮKassa → сервер с ботом → проверка**.

---

## 0. Что понадобится

| Что | Зачем |
|---|---|
| VPS для бота: 1–2 vCPU, 2 ГБ RAM, Ubuntu 22.04/24.04 | бот + PostgreSQL + Redis (можно на одном сервере с панелью, но лучше отдельно) |
| Серверы под ноды (по одному на страну/локацию) | сам VPN-трафик |
| Домены (A-записи на IP серверов): `panel.example.com`, `bot.example.com` | панель и публичный адрес бота (HTTPS обязателен) |
| Аккаунт ЮKassa | приём платежей |
| Telegram-аккаунт владельца | управление ботом |

---

## 1. Telegram

1. **@BotFather** → `/newbot` → получи **токен** → `BOT_TOKEN`.
2. **@userinfobot** → узнай свой числовой ID → `OWNER_IDS` (несколько владельцев — через запятую).
3. Заведи аккаунт/чат поддержки → ссылка вида `https://t.me/your_support` → `SUPPORT_URL` (потом меняется в админке).
4. В @BotFather: `/setdescription`, `/setuserpic` — на твой вкус. Команды бот выставляет сам.

---

## 2. Панель Remnawave

Документация: <https://docs.rw/install/remnawave-panel>. Если панель уже стоит — переходи к п. 2.3.

### 2.1 Установка (кратко, из официальной инструкции)

```bash
sudo curl -fsSL https://get.docker.com | sh
mkdir /opt/remnawave && cd /opt/remnawave
curl -o docker-compose.yml https://raw.githubusercontent.com/remnawave/backend/refs/heads/main/docker-compose-prod.yml \
 && curl -o .env https://raw.githubusercontent.com/remnawave/backend/refs/heads/main/.env.sample

# секреты и пароль БД
sed -i "s/^APP_SECRET=.*/APP_SECRET=$(openssl rand -hex 64)/" .env \
 && sed -i "s/^METRICS_PASS=.*/METRICS_PASS=$(openssl rand -hex 64)/" .env \
 && sed -i "s/^WEBHOOK_SECRET_HEADER=.*/WEBHOOK_SECRET_HEADER=$(openssl rand -hex 64)/" .env
pw=$(openssl rand -hex 24) && sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$pw/" .env \
 && sed -i "s|^\(DATABASE_URL=\"postgresql://postgres:\)[^\@]*\(@.*\)|\1$pw\2|" .env
```

В `.env` панели задай: `FRONT_END_DOMAIN=panel.example.com` и `SUB_PUBLIC_DOMAIN=panel.example.com/api/sub`.

```bash
docker compose up -d && docker compose logs -f -t
```

### 2.2 Реверс-прокси (обязателен)

Панель слушает только `127.0.0.1` и **без реверс-прокси работать не будет**. Настрой Caddy/Nginx по инструкции
<https://docs.rw/install/reverse-proxies/> (HTTPS на `panel.example.com`).
Про защиту панели (cookie / Caddy Security) — <https://docs.rw/install/panel-security/>.

### 2.3 Ноды, хосты, сквады — основа ваших тарифов

1. Установи ноды: <https://docs.rw/install/remnawave-node> (по одной на локацию).
2. В панели создай Config Profile и Hosts. **Локации** для пользователя = хосты.
3. Создай **два Internal Squad**: `LITE` и `PREMIUM`.
   * В `LITE` включи inbound'ы, чьи хосты дают **5 локаций**.
   * В `PREMIUM` — **10 локаций**, включая хосты «обхода белых списков» (их **не** добавляй в LITE).
4. Подписку клиенту отдаёт панель по адресу `https://panel.example.com/api/sub/<shortUuid>`
   (отдельная страница подписок — по желанию: <https://docs.rw/install/remnawave-subscription-page>).

### 2.4 Включи лимит устройств (HWID)

Панель → **Subscription → Settings → HWID Device Limit** → включить.
По умолчанию функция **выключена** — без неё лимит 3/5 устройств не будет работать.
Бот сам выставляет каждому пользователю лимит по тарифу (LITE — 3, PREMIUM — 5).
Работает с клиентами, которые отправляют HWID — **Happ** их отправляет: <https://docs.rw/features/hwid-device-limit>.

### 2.5 API-токен для бота

Панель → **Settings → API Tokens → Create**. Выдай права (scopes) на пользователей, HWID-устройства,
внутренние сквады, ноды и системную статистику — проще всего отметить **все**.
Токен показывается **один раз** — сразу скопируй в `REMNAWAVE_TOKEN`. Права токена потом не редактируются (только пересоздать).

### 2.6 (рекомендуется) Вебхуки панели → бот

Нужны для: мгновенной награды за приглашённого (`user.first_connected`) и алертов о падении ноды
(`node.connection_lost` / `node.connection_restored`). Без них награды всё равно начисляются — проверкой раз в ~3 минуты.

В `.env` панели:

```
WEBHOOK_ENABLED=true
WEBHOOK_URL=https://bot.example.com/webhooks/remnawave/<WEBHOOK_SECRET бота>
WEBHOOK_SECRET_HEADER=<значение, уже сгенерированное при установке>
```

Это же значение `WEBHOOK_SECRET_HEADER` впиши в `.env` бота как `REMNAWAVE_WEBHOOK_SECRET`, затем `docker compose up -d` в папке панели.
Подпись проверяется по HMAC-SHA256 (<https://docs.rw/features/webhooks>).

---

## 3. ЮKassa

1. Зарегистрируйся на <https://yookassa.ru>, подай заявку на подключение магазина (ИП / ООО / самозанятый). Дождись проверки.
   Для отладки сначала используй **тестовый магазин** — в нём реальные деньги не списываются (тестовые карты — в разделе «Тестирование» документации ЮKassa).
2. **Ключи:** Интеграция → Ключи API → скопируй `shopId` → `YOOKASSA_SHOP_ID` и выпусти секретный ключ → `YOOKASSA_SECRET_KEY`.
3. **Уведомления:** Интеграция → HTTP-уведомления → URL
   `https://bot.example.com/webhooks/yookassa/<WEBHOOK_SECRET бота>`, события: `payment.succeeded`, `payment.canceled`, `refund.succeeded`.
   (Подписи у уведомлений нет — поэтому бот не верит присланному, а сам запрашивает платёж у ЮKassa и сверяет сумму. Если уведомление потерялось, статус добирается воркером за ~1–2 минуты.)
4. **Чеки (54-ФЗ).** ИП/ООО — подключи «Чеки от ЮKassa» и задай `YOOKASSA_VAT_CODE` (и `YOOKASSA_TAX_SYSTEM_CODE`, если систем налогообложения несколько).
   Самозанятый — включи автоотправку чеков в «Мой налог» в личном кабинете ЮKassa. Бот попросит у клиента email и передаст данные чека.
   Если чеки тебе не нужны — `YOOKASSA_RECEIPTS=false` (или в админке: Настройки → Чеки).
5. Описание магазина/оферта: ЮKassa просит публичные условия оказания услуг. Подготовь страницу с офертой.

> ⚠️ **Риск, о котором лучше знать заранее.** Платёжные сервисы могут отказать в подключении или приостановить магазин для VPN-сервисов, правила и практика меняются. Уточни актуальные требования у поддержки ЮKassa **до** запуска, описывай деятельность магазина честно и держи запасной способ приёма оплаты. Реклама VPN в России ограничена — учитывай это при продвижении. Это не юридическая консультация; для рисков по закону и персональным данным (ID Telegram, email) нужен юрист.

---

## 4. Сервер с ботом

```bash
sudo curl -fsSL https://get.docker.com | sh
mkdir -p /opt/fable && cd /opt/fable
# загрузи сюда содержимое архива fable-vpn-bot (scp / git)
cp .env.example .env
nano .env
```

Обязательно заполни: `BOT_TOKEN`, `OWNER_IDS`, `BOT_DOMAIN`, `PUBLIC_BASE_URL`, `WEBHOOK_SECRET`, `POSTGRES_PASSWORD`,
`REMNAWAVE_URL`, `REMNAWAVE_TOKEN`, `YOOKASSA_SHOP_ID`, `YOOKASSA_SECRET_KEY`.
Секреты: `openssl rand -hex 24`.

### HTTPS для домена бота

**Вариант A — на сервере нет другого реверс-прокси (80/443 свободны):** используй встроенный Caddy (сам получит сертификат):

```bash
docker compose --profile caddy up -d --build
```

**Вариант B — прокси уже есть** (например, тот же сервер, что и панель): запусти бота без Caddy
`docker compose up -d --build` и добавь сайт в свой прокси, направив его на `127.0.0.1:8080`:

```
# Caddy
bot.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

```nginx
# Nginx
server {
    listen 443 ssl;
    server_name bot.example.com;
    # ssl_certificate / ssl_certificate_key — как у остальных сайтов
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

(Если твой прокси сам запущен в Docker, `127.0.0.1` для него — он сам; используй адрес хоста, например `172.17.0.1:8080`.)

Проверка: `curl https://bot.example.com/healthz` → `{"ok": true}`, а `https://bot.example.com/dl` открывает страницу скачивания Happ.

### Бот подключается к панели

* Панель на другом сервере / по домену: `REMNAWAVE_URL=https://panel.example.com` (проще всего).
* Если панель закрыта защитой (cookie / Caddy Security) — задай `REMNAWAVE_COOKIE=имя=значение` или `REMNAWAVE_CADDY_KEY=…`, либо разреши IP сервера бота в настройках защиты.
* Бот всегда добавляет заголовки `X-Forwarded-For` и `X-Forwarded-Proto: https` — без них Remnawave рвёт соединение при прямом доступе по http.

---

## 5. Первый запуск и проверка (чек-лист)

1. `docker compose logs -f bot` — должно появиться «Бот: @имя», владельцу придёт «🟢 Fable | VPN запущен».
2. В боте: `/admin` → **Система → Диагностика**. Всё должно быть ✅ (PostgreSQL, Redis, Remnawave, ЮKassa). Прочитай блок «Что стоит поправить».
3. **Тарифы → LITE → Сквады**: отметь сквад `LITE`. То же для PREMIUM. *Без этого подписчики не получат серверы.*
   При необходимости поправь цены, лимиты, описание (все тексты — в разделе «Тексты»).
4. **Настройки**: ссылка поддержки, пробный период (по умолчанию 7 дней PREMIUM), напоминания, реферальная награда (по умолчанию +7 дней LITE).
5. **Прогон глазами пользователя** (со второго аккаунта Telegram):
   `/start` → «Подключиться» → «Скачать Happ» → «Добавить подписку» → включи VPN в Happ.
   В админке в карточке пользователя появятся устройство, трафик и «онлайн».
6. **Оплата в тестовом магазине:** купи LITE → после оплаты придёт уведомление. Затем:
   * купи PREMIUM поверх LITE — бот предложит доплату `(249−149) × остаток/30 + 249` и два варианта;
   * купи LITE поверх PREMIUM — LITE встанет в очередь после PREMIUM.
7. **Рефералка:** возьми ссылку в «Реферальная система», зайди по ней третьим аккаунтом, подключи VPN и потрать немного трафика — пригласившему придёт +7 дней LITE.
8. **Боевой режим:** замени тестовые ключи ЮKassa на боевые в `.env`, `docker compose up -d`. Сделай первую настоящую оплату на минимальную сумму и проверь чек.

---

## 6. Обслуживание

| Задача | Как |
|---|---|
| Логи | `docker compose logs -f --tail=200 bot` · в боте: Система → Журнал ошибок |
| Обновить код | замени файлы → `docker compose up -d --build` |
| Бэкап БД | автоматически раз в сутки владельцам в Telegram (час — `BACKUP_HOUR`) · вручную: Система → Бэкап БД |
| Восстановление | `docker compose stop bot && docker compose exec -T postgres pg_restore -U fable -d fable --clean --if-exists < backup.dump && docker compose start bot` |
| Ссылки на скачивание Happ | файл `app/web/pages.py` (словарь `LINKS`), официальный список: <https://www.happ.su/main/readme> |
| Изменить тексты, цены, промокоды, рассылки | всё в боте: `/admin` |

---

## 7. Частые проблемы

| Симптом | Что проверить |
|---|---|
| Диагностика: «Remnawave ❌» | `REMNAWAVE_URL` (https и домен), токен и его права, защита панели (cookie / `X-Api-Key`), версия панели 3.x |
| «Добавить подписку» не открывает Happ | Happ не установлен; на странице есть кнопка «Открыть Happ» и ссылка для ручной вставки. Страница лучше всего работает в браузере |
| Устройство не появляется в списке | включён ли HWID в панели (п. 2.4), клиент — Happ (или другой с поддержкой HWID) |
| Подписка не включилась после оплаты | Админка → Платежи → платёж → «Проверить в ЮKassa»; убедись, что вебхук ЮKassa настроен и HTTPS домена бота работает |
| Платёж отклоняется при создании | в ЮKassa включена фискализация, а `YOOKASSA_RECEIPTS=false` (или наоборот); неверный `YOOKASSA_VAT_CODE` |
| «Не синхронизирован с панелью» в карточке | панель была недоступна — воркер повторит сам; можно нажать «🔄 Синхронизировать» |
| Награда за друга не пришла | друг должен реально подключиться (первое подключение или ≥ порога трафика); Рефералка → Ожидающие → «Начислить» вручную |

---

## 8. Что дальше (идеи)

* Автопродление по сохранённому способу оплаты ЮKassa (рекуррентные платежи).
* Второй платёжный провайдер (СБП-агрегатор, криптоплатежи) как запасной — слой платежей уже отделён от логики подписок.
* Шифрованные ссылки подписки Happ (скрывают URL подписки от пользователя).
* Миграции схемы БД через Alembic, когда начнёшь менять структуру таблиц (сейчас таблицы создаются автоматически при первом запуске).
