# -*- coding: utf-8 -*-
"""
MCP-сервер доступа к порталу 1С:ИТС (its.1c.ru).

Учётные данные читаются из текстового файла (формат: ITS_USER=... и
ITS_PASS=..., по одной на строку). Файл ищется по порядку:

1. путь из переменной окружения ITS_CRED_FILE;
2. ~/.1c-its/its_credentials.txt в домашней папке пользователя;
3. its_credentials.txt в текущей папке.

Значения не отдаются наружу: ни в аргументах инструментов, ни в
ответах, ни в текстах ошибок.

Запуск (stdio-транспорт): mcp-1c-its  или  python -m mcp_1c_its.server
"""
import hashlib
import html as html_lib
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx
import trafilatura
from mcp.server.fastmcp import FastMCP

CRED_FILE_NAME = "its_credentials.txt"
CONFIG_DIR = Path.home() / ".1c-its"
CACHE_DIR = CONFIG_DIR / "cache"

ITS_USER = ""
ITS_PASS = ""


def _cred_candidates():
    """Где искать файл учётных данных, в порядке приоритета."""
    env = os.environ.get("ITS_CRED_FILE")
    if env:
        yield Path(env)
    yield CONFIG_DIR / CRED_FILE_NAME
    yield Path.cwd() / CRED_FILE_NAME


def _load_credentials():
    """Читает файл учётных данных.

    Возвращает источник (для статуса, без значений).
    """
    global ITS_USER, ITS_PASS
    for f in _cred_candidates():
        if not f.exists():
            continue
        from_file = {}
        try:
            try:
                text = f.read_text(encoding="utf-8-sig")
            except UnicodeDecodeError:
                text = f.read_text(encoding="cp1251")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            from_file[key.strip().upper()] = val.strip().strip('"').strip("'")
        ITS_USER = from_file.get("ITS_USER", "")
        ITS_PASS = from_file.get("ITS_PASS", "")
        if ITS_PASS:
            return f"файл {f}"
    return (f"не заданы (создайте {CONFIG_DIR / CRED_FILE_NAME} "
            f"или задайте ITS_CRED_FILE)")


_CRED_SOURCE = _load_credentials()

BASE = "https://its.1c.ru"
LOGIN_BASE = "https://login.1c.ru"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

CACHE_TTL = 7 * 24 * 3600    # материалы, сек
SEARCH_TTL = 24 * 3600       # выдача поиска, сек
MIN_INTERVAL = 1.0           # пауза между запросами к ИТС, сек

# Разделы ИТС для поиска: код базы -> о чём она.
# Код берётся из адреса материала (/db/<код>/content/...); в its_search можно
# передать и раздел, которого нет в перечне, - список не ограничивающий.
SECTION_GROUPS = [
    ("Поиск по всему порталу", {
        "morphmerged": "все разделы ИТС сразу (значение по умолчанию)",
    }),
    ("Платформа и разработка", {
        "v8std": "стандарты разработки конфигураций: как писать код и метаданные",
        "metod8dev": "методическая поддержка разработчиков и администраторов: обмен данными, "
                     "производительность, администрирование, клиент-сервер",
        "v8327doc": "документация платформы 8.3.27 (встроенный язык, объекты, механизмы)",
        "v8326doc": "документация платформы 8.3.26",
        "v851doc": "документация платформы 8.5.1",
        "v854doc": "документация платформы 8.5.4 (тестовая версия)",
        "pubdevguideedt": "практическое пособие разработчика на 1C:EDT: пошаговые примеры",
        "v8devgloss": "глоссарий разработчика: значения терминов платформы",
        "answers1c": "«Отвечает специалист 1С»: разбор частных вопросов по платформе",
        "metod81": "методическая поддержка 1С:Предприятия 8: типовые приёмы и рекомендации",
    }),
    ("Библиотеки", {
        "bsp321doc": "БСП 3.2.1: описание подсистем, процедур и параметров",
        "bsp3112doc": "БСП 3.1.12: то же для предыдущей ветки",
        "bid304doc": "библиотека интеграции с 1С:Документооборотом 3.0.4",
        "bid303doc": "библиотека интеграции с 1С:Документооборотом 3.0.3",
        "bed1101doc": "библиотека электронных документов 1.10.1 (ЭДО)",
        "uisl29doc": "библиотека интернет-поддержки пользователей 2.9",
    }),
    ("Типовые конфигурации", {
        "bp8doc": "1С:Бухгалтерия 8: руководство по ведению учёта",
        "hoosn": "справочник хозяйственных операций 1С:Бухгалтерии 8: документы под операцию",
        "docprof30": "1С:Документооборот 3.0: описание механизмов и настроек",
        "staff1c": "кадровый учёт и расчёты с персоналом в программах 1С (ЗУП)",
        "stafft": "справочник кадровика: оформление кадровых ситуаций",
        "erp26doc": "1С:ERP Управление предприятием 2.6",
        "ka26doc": "1С:Комплексная автоматизация 2.6",
    }),
    ("Учёт, налоги, законодательство", {
        "accnds": "учёт НДС в программах 1С",
        "accusn": "учёт при УСН в программах 1С",
        "taxnds": "НДС: нормы законодательства",
        "taxprib": "налог на прибыль организаций",
        "taxndfl": "НДФЛ",
        "answersacc": "ответы по учёту в коммерческих организациях",
        "answersstaff": "ответы по кадрам и оплате труда",
        "answerstax": "ответы по налогам и взносам",
    }),
    ("Релизы и справочное", {
        "updinfo": "информация об обновлениях программных продуктов 1С",
        "updlib": "информация об обновлениях стандартных библиотек",
        "sprinfo": "обучение, экзамены, справочная информация",
        "newsits": "новости ИТС",
    }),
]

mcp = FastMCP(
    "1c-its",
    instructions=(
        "Доступ к порталу 1С:ИТС (its.1c.ru) по подписке пользователя. "
        "Схема работы: its_search — найти материалы (по умолчанию по всем "
        "разделам), затем its_get — получить текст выбранного материала "
        "в Markdown по адресу (path) из результата поиска. its_sections — "
        "доступные разделы, its_status — самопроверка. Учётные данные "
        "сервер хранит у себя и наружу не отдаёт."
    ),
)

# служебные журналы не должны шуметь в потоке ошибок MCP-процесса
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("trafilatura").setLevel(logging.ERROR)

client = httpx.Client(
    timeout=40.0,
    follow_redirects=True,
    headers={"User-Agent": UA, "Accept-Language": "ru"},
    # только IPv4 и один повтор: в контейнере запрос адреса IPv6 отвечает
    # «No address associated with hostname» и запрос падает на ровном месте
    transport=httpx.HTTPTransport(local_address="0.0.0.0", retries=1),
)

_logged_in = False
_last_request = 0.0


# ---------------------------------------------------------------- служебное

def _pause():
    """Щадящий режим: не чаще одного запроса к ИТС в секунду."""
    global _last_request
    wait = MIN_INTERVAL - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def _request(method, url, **kw):
    """Запрос к ИТС с повтором: сеть контейнера изредка отвечает на запрос
    адреса «No address associated with hostname», и одиночный запрос падает."""
    last = None
    for attempt in range(3):
        _pause()
        try:
            return client.request(method, url, **kw)
        except httpx.TransportError as exc:
            last = exc
            time.sleep(1.0 * (attempt + 1))
    raise last


def _get(url, **kw):
    return _request("GET", url, **kw)


def _post(url, **kw):
    return _request("POST", url, **kw)


def _text(resp):
    """Текст ответа: ИТС отдаёт windows-1251, login.1c.ru — utf-8."""
    ct = resp.headers.get("content-type", "")
    m = re.search(r"charset=([\w-]+)", ct, re.I)
    enc = m.group(1) if m else ("cp1251" if "its.1c.ru" in str(resp.url) else "utf-8")
    return resp.content.decode(enc, errors="replace")


def _input_fields(html_str, only_hidden=False):
    """Значения полей <input> страницы (для CAS-формы входа)."""
    out = {}
    for tag in re.findall(r"<input\b[^>]*>", html_str, re.I):
        if only_hidden and not re.search(r'type\s*=\s*"hidden"', tag, re.I):
            continue
        name = re.search(r'name\s*=\s*"([^"]*)"', tag, re.I)
        val = re.search(r'value\s*=\s*"([^"]*)"', tag, re.I)
        if name:
            out[html_lib.unescape(name.group(1))] = (
                html_lib.unescape(val.group(1)) if val else "")
    return out


def _normalize_path(url_or_path):
    """'/db/<раздел>/content/<номер>...' — канонический адрес материала."""
    s = url_or_path.strip()
    s = re.sub(r"^https?://its\.1c\.ru", "", s, flags=re.I)
    m = re.match(r"^/db/[a-z0-9_]+/content/\d+(?:/hdoc)?", s, re.I)
    return m.group(0) if m else s


def _cache_key(path):
    return CACHE_DIR / (hashlib.sha1(path.encode("utf-8")).hexdigest() + ".md")


def _cache_get(path, ttl):
    f = _cache_key(path)
    try:
        if f.exists() and time.time() - f.stat().st_mtime < ttl:
            return f.read_text(encoding="utf-8")
    except OSError:
        pass
    return None


def _cache_put(path, text):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _cache_key(path).write_text(text, encoding="utf-8")
    except OSError:
        pass


def _safe(fn, *args):
    """Единая точка выхода ошибок: короткое сообщение без куков и заголовков."""
    try:
        return fn(*args)
    except Exception as exc:
        return f"Ошибка: {type(exc).__name__}: {str(exc)[:300]}"


# ------------------------------------------------------------------- вход

def _meta_refresh_url(html_str):
    """Адрес авто-перехода из <meta http-equiv="refresh">, если он есть."""
    m = re.search(r'<meta[^>]*http-equiv\s*=\s*"?refresh"?[^>]*>', html_str, re.I)
    if not m:
        return None
    c = re.search(r'content\s*=\s*"([^"]+)"', m.group(0), re.I)
    if not c:
        return None
    content = html_lib.unescape(c.group(1)).strip("'\" ")
    if ";" in content:                      # формат «0;<адрес>» или «0;url=<адрес>»
        content = content.split(";", 1)[1]
    m2 = re.match(r"(?i)\s*url\s*=\s*(.+)", content)
    if m2:
        content = m2.group(1)
    content = content.strip().strip("'\" ")
    return content or None


def _login():
    global _logged_in
    if not ITS_USER or not ITS_PASS:
        raise RuntimeError(
            "Не заданы учётные данные: создайте "
            f"{CONFIG_DIR / CRED_FILE_NAME} со строками ITS_USER=... и "
            "ITS_PASS=... (или задайте переменную окружения ITS_CRED_FILE)")
    # вход всегда с чистого листа: после прошлого входа остаётся кука TGC,
    # и login.1c.ru отдаёт не форму, а сразу билет - разбирать было бы нечего
    client.cookies.clear()
    # адрес возврата после единого входа login.1c.ru
    service = f"{BASE}/login/?action=aftercheck&provider=login"
    # 1) страница входа ИТС подсказывает актуальный service (скрытое поле)
    try:
        page = _text(_get(f"{BASE}/user/auth", params={"backurl": "/"}))
        m = (re.search(r'name="service"[^>]*value="([^"]+)"', page)
             or re.search(r'action="(https://login\.1c\.ru/login\?[^"]+)"',
                          page, re.I))
        if m:
            service = html_lib.unescape(m.group(1))
    except Exception:
        pass  # остаётся service по умолчанию
    # 2) форма CAS: скрытые поля execution/_eventId и пр.
    form_page = _text(_get(f"{LOGIN_BASE}/login", params={"service": service}))
    fields = _input_fields(form_page, only_hidden=True)
    if "execution" not in fields:
        raise RuntimeError("Не получена форма входа login.1c.ru — "
                           "разметка изменилась")
    fields.update({"username": ITS_USER, "password": ITS_PASS, "rememberMe": "on"})
    # 3) отправка учётных данных; цепочка редиректов вернёт на ИТС с сессией
    resp = _post(f"{LOGIN_BASE}/login", data=fields)
    # итоговая страница ИТС завершает вход авто-переходом (meta refresh)
    for _ in range(3):
        nxt = _meta_refresh_url(_text(resp))
        if not nxt:
            break
        resp = _get(nxt if re.match(r"^https?://", nxt, re.I) else f"{BASE}{nxt}")
    ok = "its.1c.ru" in str(resp.url) and "login.1c.ru" not in str(resp.url)
    if ok:
        probe = _text(_get(f"{BASE}/db/v8std"))
        ok = "Выйти" in probe  # вошедшему показывается выход; анонимно слова нет
    if not ok:
        raise RuntimeError("Вход в ИТС не удался: неверный логин/пароль "
                           "или изменилась форма входа login.1c.ru")
    _logged_in = True


def _ensure_session(force=False):
    if _logged_in and not force:
        return
    _login()


# ------------------------------------------------------------ инструменты

def search_raw(query, section="morphmerged"):
    section = (section or "morphmerged").strip("/")
    key = f"search:{section}:{query.lower()}"
    cached = _cache_get(key, SEARCH_TTL)
    if cached:
        return cached
    # ИТС ждёт запрос в кодировке windows-1251
    q = quote(query.encode("cp1251", errors="replace"))
    resp = _get(f"{BASE}/db/{section}/search/all?query={q}")
    if resp.status_code != 200:
        return f"Ошибка поиска ИТС: HTTP {resp.status_code}"
    html_str = _text(resp)
    found, seen = [], set()
    for tag, inner in re.findall(r'(<a\b[^>]*search_link[^>]*>)(.*?)</a>',
                                 html_str, re.I | re.S):
        href = re.search(r'href="([^"]+)"', tag, re.I)
        if not href:
            continue
        path = _normalize_path(html_lib.unescape(href.group(1)))
        if path in seen:
            continue
        seen.add(path)
        title = html_lib.unescape(re.sub(r"<[^>]+>", "", inner)).strip()
        found.append(f"- {title} — {path}")
    if not found:
        return ("Ничего не найдено (или указан неверный раздел; "
                "список разделов — its_sections).")
    result = (f"Поиск «{query}» в разделе {section}, ссылок: {len(found)}\n"
              + "\n".join(found[:30]))
    _cache_put(key, result)
    return result


def get_raw(url_or_path):
    path = _normalize_path(url_or_path)
    cached = _cache_get(path, CACHE_TTL)
    if cached:
        return cached
    _ensure_session()
    resp = _get(f"{BASE}{path}")
    if "login.1c.ru" in str(resp.url):  # сессия истекла — перезаход и повтор
        _login()
        resp = _get(f"{BASE}{path}")
    if resp.status_code != 200:
        return f"Ошибка ИТС: HTTP {resp.status_code} по адресу {path}"
    html_str = _text(resp)
    t = re.search(r"<title[^>]*>([^<]+)</title>", html_str, re.I)
    title = html_lib.unescape(t.group(1)).strip() if t else path
    # основной текст материала ИТС лежит в отдельном файле внутри iframe
    frame = re.search(
        r'<iframe[^>]+id="w_metadata_doc_frame"[^>]+src="([^"]+)"', html_str, re.I)
    body_html = html_str
    if frame:
        src = html_lib.unescape(frame.group(1))
        doc = _get(f"{BASE}{src}" if src.startswith("/") else src)
        if doc.status_code == 200:
            body_html = _text(doc)
    md = trafilatura.extract(body_html, output_format="markdown",
                             include_links=True, include_tables=True) or ""
    if len(md) < 100:
        md = "(не удалось извлечь основной текст страницы)"
    result = f"# {title}\nИсточник: {BASE}{path}\n\n{md}"
    _cache_put(path, result)
    return result


@mcp.tool()
def its_search(query: str, section: str = "morphmerged") -> str:
    """Поиск по порталу 1С:ИТС. Возвращает заголовки и адреса (path)
    найденных материалов; текст материала — инструментом its_get.
    section — раздел ИТС (список: its_sections), по умолчанию morphmerged
    (по всем разделам)."""
    return _safe(search_raw, query, section)


@mcp.tool()
def its_get(path: str) -> str:
    """Материал ИТС в виде Markdown. path — адрес из результата its_search,
    например /db/v8std/content/777; можно передать и полный URL."""
    return _safe(get_raw, path)


@mcp.tool()
def its_sections() -> str:
    """Разделы ИТС для its_search: код раздела и что в нём искать.
    Перечень не ограничивающий - раздел можно взять и из адреса материала
    (/db/<код>/content/...), даже если его здесь нет."""
    out = []
    for title, block in SECTION_GROUPS:
        out.append(f"## {title}")
        out.extend(f"- {code} - {about}" for code, about in block.items())
        out.append("")
    return "\n".join(out).strip()


@mcp.tool()
def its_status() -> str:
    """Самопроверка сервера: откуда взяты учётные данные и заданы ли они
    (значения не раскрываются), активна ли сессия ИТС."""
    lines = [f"Источник учётных данных: {_CRED_SOURCE}",
             f"ITS_USER задан: {bool(ITS_USER)}",
             f"ITS_PASS задан: {bool(ITS_PASS)}"]
    try:
        _ensure_session()
        lines.append("Сессия ИТС: активна")
    except Exception as exc:
        lines.append(f"Сессия ИТС: ошибка — {exc}")
    return "\n".join(lines)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
