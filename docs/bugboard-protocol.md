# bugboard.1c.ru: внутренний протокол (реверс-инжиниринг)

Публичного API у «1С:Публикация ошибок» (bugboard.1c.ru) нет. Ниже —
восстановленная схема доступа теми же учётными данными, что и для ИТС.
Статус: вход и вызов серверных методов проверены вживую (2026-09-07),
подбор имён модулей для части методов — в работе.

## 1. Вход (без пароля, поверх сессии ИТС)

bugboard.redirect на единый вход 1C:ID (auth.1cmycloud.com), но её
основной провайдер — CAS `login.1c.ru`, где уже есть кука TGC после
входа в ИТС. Цепочка (все запросы одним httpx-клиентом, с куками):

1. `_ensure_session()` — обычный вход в ИТС (TGC на login.1c.ru).
2. `GET https://bugboard.1c.ru/` — редирект на
   `auth.1cmycloud.com/auth/v2/server/signin?app_req_id=…&app_id=…`;
   запомнить оба параметра.
3. `GET https://auth.1cmycloud.com/auth/v2/server/signin/capabilities`
   с заголовком `X-App-Req-Id: <app_req_id>` → JSON с
   `authServices.primary[serviceType=="CAS"]` — взять `userListId`,
   `serviceId` (для bugboard: `81c1dc80-…`, `6bc8fb1a-…`).
4. `GET https://auth.1cmycloud.com/auth/v2/client/cas/<userListId>/<serviceId>?app_id=<app_id urlencoded>&app_req_id=<app_req_id>`
   — важно: `app_id` обязательно URL-кодировать (в конце `=`);
   без `app_id` билет примется «в никуда», с некодированным — 500.
   Переходы: 302 → login.1c.ru (тихий билет по TGC) → 302 → страница
   `oauth2/external` со скрытыми полями `token`, `appId`, `appReqId`.
5. `POST` на URL этой страницы (`/auth/v2/server/oauth2/external?…`)
   формой `app_id`, `app_req_id`, `token` + заголовок `X-App-Req-Id`
   — в ответе текстом адрес `…/oauth2/authorize?…`.
6. `GET` этого адреса — bugboard ставит куки `__app_id`, `__auth_id`.
   Готово.

## 2. Приложение и метаданные

bugboard — приложение «1С:Элемент» (Element Server, G5 runtime).
Оболочка `GET /` содержит глобальные `__gSrv_APP_HASH` и
`__gSrv_SRV_VERSION` — они нужны в каждом запросе.

Метаданные (все типы, 93 серверных метода):

```
GET /sys/get-component-with-deps
    ?componentName=e1c::bugboard::Основное::ПубликацияОшибок   (urlencode)
    &clientMdVersion=<urlencode("APP_HASH,SRV_VERSION")>
X-G5-Version: <то же, что clientMdVersion>
```

Ответ ~1.3 МБ (несколько JSON-документов подряд — парсить с первого `{`
через raw_decode). Внутри: узлы типов, у каждого `serverIface` /
`serverContextualIface` с `name`, `paramTypes`, `paramNames`,
`returnTypes`. Полезные методы (владелец — тип `…Багборд::Проекты` и
`…Компоненты::ПоискДанных::ПоискОшибок`):

- `ВыполнитьПоискПоОшибкам(СтрокаПоиска: Std::String)` — глобальный поиск;
- `НайтиОшибкиПоКоду(Код)` — ошибка по номеру `EF_…`;
- `НайтиПроектПоКоду(Код)` — проект по коду (`bp3`);
- `ПолучитьМассивОшибокВВерсии(Проект, Версия)` — ошибки версии;
- `ИсправленныеОшибкиМеждуВерсиями`, `ВыгрузитьОшибкиВВерсииВJSON`,
  `ПолучитьДанныеДерева`, `ДоступныеВерсии` и др.

## 3. Вызов серверного метода (проверено, HTTP 200)

```
POST https://bugboard.1c.ru/ui/module/call
X-G5-Version: <urlencode("APP_HASH,SRV_VERSION")>
Content-Type: application/json

{
  "moduleName": "e1c::bugboard::Компоненты::ПоискДанных::ПоискОшибок",
  "methodName": "ВыполнитьПоискПоОшибкам",
  "parameters": [{"type": "Std::String", "value": "<запрос>"}],
  "remoteCallerInfo": {"id": "<uuid>", "debugState": null, "bslStack": []}
}
```

Ответ: `{"result": {"type": …, "value": …}, "debugExitReason": "NONE"}` —
типизированные значения вида `{"type": "<полное имя типа>", "value": …}`;
массивы — `{"items": [...]}`.

Проверено: поиск отвечает 200 (пока пустым списком — вероятно, нужны
контекстные параметры или другой формат строки; подобрать). Методы
владельца `…Багборд::Проекты` пока дают 500 — имя модуля в
`moduleName` не то (у `ПоискОшибок` совпало с владельцем; для `Проекты`
взять имя модуля из узла метаданных: `modules.module[].name`).

## 4. Прочие конечные точки (из кода приложения, чанк 205.js)

`module/contextualCall` (метод с контекстом экземпляра),
`dynamic-list` / `dynamic-list-info` (списки), `entity/read` (карточка),
`search-substring`, `entity/create|update|delete`, `pdf/*`,
`ecs-application/getinfo`, `auth/status`.

Чанк приложения: `/static/applications/bundled/205.js?hash=<hash из оболочки>`.

## 5. План гибрида

1. Прямой путь — `module/call` (быстро, лёгкий пакет). Имена модулей
   добывать из метаданных автоматически.
2. Если прямой путь падает (обновление bugboard) — резерв:
   headless-браузер (Playwright, необязательная зависимость
   `pip install mcp-1c-its[browser]`): вход той же цепочкой (куки
   переносятся в контекст браузера внутри процесса), рендер `?state=…`,
   разбор DOM. Инструмент при этом возвращает пометку «прямой протокол
   недоступен, работает браузерный режим — обновите протокол».
