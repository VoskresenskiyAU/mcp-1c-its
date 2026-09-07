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
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

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
RELEASES_BASE = "https://releases.1c.ru"
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
        "доступные разделы, its_status — самопроверка. Дополнительно: "
        "releases_products, releases_patches и releases_history — версии, "
        "исправления (EF_...) и историю релизов типовых конфигураций "
        "с releases.1c.ru; bugboard_card, "
        "bugboard_version_errors и bugboard_versions — доска ошибок 1С "
        "(bugboard.1c.ru): карточка ошибки по EF-номеру, ошибки версии "
        "конфигурации, последние версии. Всё — по той же подписке. "
        "Учётные данные сервер хранит у себя и наружу не отдаёт."
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


def _strip_tags(fragment):
    """Текст из html-фрагмента: без тегов, без сплошных пробелов."""
    return html_lib.unescape(
        re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


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


# ------------------------------------------------- релизы и исправления
# releases.1c.ru входит в подписку ИТС и открывается той же учётной записью:
# после входа в ИТС кука TGC единого входа пропускает и на releases.

def _releases_page(path, **params):
    """Страница releases.1c.ru; при истёкшей сессии — перезаход в ИТС."""
    _ensure_session()
    resp = _get(f"{RELEASES_BASE}{path}", params=params or None)
    txt = _text(resp)
    if "loginForm" in txt or "login.1c.ru" in str(resp.url):
        _login()
        resp = _get(f"{RELEASES_BASE}{path}", params=params or None)
        txt = _text(resp)
    if resp.status_code != 200:
        return None, f"Ошибка releases.1c.ru: HTTP {resp.status_code}"
    return txt, None


def products_raw(query=""):
    """Конфигурации с releases.1c.ru/total: название, ник, версии."""
    key = f"products:{query.lower()}"
    cached = _cache_get(key, SEARCH_TTL)
    if cached:
        return cached
    txt, err = _releases_page("/total")
    if err:
        return err
    out, seen = [], set()
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", txt, re.S):
        m_nick = re.search(r"nick=([A-Za-z0-9_]+)", tr)
        if not m_nick:
            continue
        nick = m_nick.group(1)
        if nick in seen:
            continue
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        name = _strip_tags(tds[0]) if tds else ""
        if not name:
            continue
        seen.add(nick)
        # ячейки: название | актуальная версия | дата выхода | план. версии |
        # ориент. сроки | даты обновления плана | ознакомит. | план ознакомит.
        actual = re.findall(r"\d+\.\d+\.\d+\.\d+", tds[1])[0] if len(tds) > 1 and re.findall(r"\d+\.\d+\.\d+\.\d+", tds[1]) else "?"
        date = _strip_tags(tds[2]) if len(tds) > 2 else ""
        if re.match(r"^\d{2}\.\d{2}\.\d{2}$", date):   # 31.08.26 -> 31.08.2026
            date = date[:-2] + "20" + date[-2:]
        line = f"- {name} — {nick} — актуальная {actual}"
        if date:
            line += f" от {date}"
        planned = _strip_tags(tds[3]) if len(tds) > 3 else ""
        if planned and "?" not in planned:
            line += f" | планируется: {planned}"
        if query and query.lower() not in line.lower():
            continue
        out.append(line)
    if not out:
        return ("Ничего не найдено. Уточните запрос: например, "
                "«Бухгалтерия», «Зарплата», «ERP».")
    result = (f"Продукты releases.1c.ru"
              f"{' по запросу «' + query + '»' if query else ''}, "
              f"найдено: {len(out)}\n" + "\n".join(out[:50]))
    _cache_put(key, result)
    return result


def _history_versions(nick):
    """Релизы проекта с /project/<nick>, свежие сверху:
    [(версия, дата, список_обновления_с, мин_платформа), …]."""
    txt, err = _releases_page(f"/project/{nick}")
    if err:
        raise RuntimeError(err)
    out = []
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", txt, re.S):
        tds = [re.findall(r"\d+\.\d+\.\d+\.\d+", td) for td in
               re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(tds) < 4 or not tds[0]:
            continue
        m = re.search(r"\d{2}\.\d{2}\.\d{2,4}", tr)
        date = m.group(0) if m else "?"
        if re.match(r"^\d{2}\.\d{2}\.\d{2}$", date):
            date = date[:-2] + "20" + date[-2:]
        out.append((tds[0][0], date, tds[2], tds[3][0] if tds[3] else "?"))
    if not out:
        raise RuntimeError(f"Релизы проекта {nick} не найдены — проверьте код "
                           "продукта (releases_products).")
    return out


def history_raw(nick, limit=15):
    """История релизов проекта с releases.1c.ru/project/<nick>."""
    nick = (nick or "").strip()
    limit = max(1, min(int(limit), 40))
    key = f"history:{nick}:{limit}"
    cached = _cache_get(key, SEARCH_TTL)
    if cached:
        return cached
    rows = []
    for ver, date, update_from, pf in _history_versions(nick):
        upd = (f"обновление с {update_from[0]}"
               + (f" и ещё {len(update_from) - 1}" if len(update_from) > 1 else "")
               if update_from else "без ограничения версии")
        rows.append(f"- {ver} от {date} ({upd}; платформа ≥ {pf})")
    result = (f"Релизы {nick} (свежие сверху), показано "
              f"{min(limit, len(rows))} из {len(rows)}:\n"
              + "\n".join(rows[:limit]))
    _cache_put(key, result)
    return result


def where_fixed_raw(nick, number, depth=8):
    """Где исправление доступно: в каких версиях — багфиксом, в какой
    версии оно встроено в сам релиз."""
    nick = (nick or "").strip()
    num = re.sub(r"(?i)^EF_", "", (number or "").strip()).replace("_", "-")
    depth = max(2, min(int(depth), 15))
    key = f"wherefixed:{nick}:{num}:{depth}"
    cached = _cache_get(key, SEARCH_TTL)
    if cached:
        return cached
    versions = [v for v, *_ in _history_versions(nick)][:depth]
    present = []
    for ver in versions:
        if num in patches_raw(nick, ver):   # списки кэшируются сами
            present.append(ver)
    if not present:
        return (f"Исправление {num} не найдено в багфиксах {len(versions)} "
                f"последних версий {nick}. Возможно, оно старше — увеличьте "
                "depth, или номера на bugboard/releases различаются.")
    # версии свежие сверху: если фикс есть в самой свежей — он ещё не вшит
    # в релиз; первая версия выше самой свежей с фиксом — релиз с фиксом
    idx_newest_with = min(versions.index(v) for v in present)
    if idx_newest_with == 0:
        verdict = (f"Исправление {num} пока доступно только как багфикс — "
                   f"в состав релиза ещё не вошло (есть в багфиксах: "
                   f"{', '.join(present)}).")
    else:
        built_in = versions[idx_newest_with - 1]
        verdict = (f"Исправление {num} встроено в релиз {built_in}; "
                   f"для более старых версий доступно багфиксами: "
                   f"{', '.join(present)}.")
    _cache_put(key, verdict)
    return verdict


def patches_raw(nick, ver):
    """Исправления (баг-фиксы) версии конфигурации с releases.1c.ru."""
    key = f"patches:{nick}:{ver}"
    cached = _cache_get(key, SEARCH_TTL)
    if cached:
        return cached
    txt, err = _releases_page("/patches/total", nick=nick, ver=ver)
    if err:
        return err
    rows = []
    for tr in re.findall(r"<tr[^>]*>.*?</tr>", txt, re.S):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(tds) < 4:
            continue
        name, descr, date = (_strip_tags(tds[1]), _strip_tags(tds[2]),
                             _strip_tags(tds[3]))
        if not name:
            continue
        line = f"- {name} ({date})"
        if descr:
            line += f" — {descr}"
        rows.append(line)
    if not rows:
        return (f"Исправления для {nick} версии {ver} не найдены. "
                "Проверьте ник (releases_products) и номер версии.")
    result = (f"Исправления {nick} версия {ver}, всего: {len(rows)}\n"
              + "\n".join(rows[:60]))
    _cache_put(key, result)
    return result


# ---------------------------------------------------------------- bugboard
# «1С:Публикация ошибок» (bugboard.1c.ru) — приложение 1С:Элемент.
# Вход — тихий CAS-переход через сессию ИТС, данные — вызовы серверных
# методов POST /ui/module/call. Полный протокол: docs/bugboard-protocol.md.

BB_BASE = "https://bugboard.1c.ru"
BB_AUTH = "https://auth.1cmycloud.com"
BB_ROUTING = "e1c::bugboard::Основное::Роутинг"
BB_VERSIONS = "e1c::bugboard::Багборд::Версии"
BB_REF_PROJECT = "e1c::bugboard::Багборд::Проекты.Reference"
BB_REF_VERSION = "e1c::bugboard::Багборд::Версии.Reference"
BB_REF_ERROR = "e1c::bugboard::Багборд::Ошибки.Reference"

BB_STATUS = [
    "Принята к исправлению", "Исправлена",
    "Планируется исправление в будущих версиях", "На рассмотрении",
    "Отложена", "Отклонена", "Отсутствует в будущей версии",
    "Статус не указан", "Запланированное поведение",
]

_bb_logged_in = False
_bb_ver = ""


def _bb_login():
    """Вход на bugboard той же учётной записью, что и в ИТС (без пароля)."""
    global _bb_logged_in, _bb_ver
    _ensure_session()  # кука TGC единого входа
    r = _get(f"{BB_BASE}/")
    if "auth.1cmycloud.com" not in str(r.url):
        pass  # сессия ещё жива — сразу снимем версию приложения
    else:
        q = parse_qs(urlparse(str(r.url)).query)
        app_req_id = q.get("app_req_id", [""])[0]
        app_id = q.get("app_id", [""])[0]
        caps = _get(f"{BB_AUTH}/auth/v2/server/signin/capabilities",
                    headers={"X-App-Req-Id": app_req_id}).json()
        cas = next((s for grp in ("primary", "secondary", "rest")
                    for s in caps.get("authServices", {}).get(grp, [])
                    if s.get("serviceType") == "CAS"), None)
        if not cas:
            raise RuntimeError("bugboard: CAS-провайдер входа не найден")
        # app_id обязательно кодировать: в конце base64 стоит '='
        r = _get(f"{BB_AUTH}/auth/v2/client/cas/{cas['userListId']}/"
                 f"{cas['serviceId']}"
                 f"?app_id={quote(app_id, safe='')}&app_req_id={app_req_id}")
        page = _text(r)
        grab = lambda i: re.search(rf'id="{i}"[^>]*>([^<]+)<', page).group(1)
        resp = _post(str(r.url),
                     data={"app_id": grab("appId"),
                           "app_req_id": grab("appReqId"),
                           "token": grab("token")},
                     headers={"X-App-Req-Id": grab("appReqId")})
        finish = resp.text.strip().strip('"')
        if finish.startswith("http"):
            _get(finish)
    shell = _text(_get(f"{BB_BASE}/"))
    h = re.search(r"__gSrv_APP_HASH = '([^']+)'", shell)
    v = re.search(r"__gSrv_SRV_VERSION = '([^']+)'", shell)
    if not h or not v:
        raise RuntimeError("bugboard: вход не завершён — обновился протокол? "
                           "См. docs/bugboard-protocol.md")
    _bb_ver = quote(f"{h.group(1)},{v.group(1)}", safe="")
    _bb_logged_in = True


def _bb_ensure(force=False):
    if _bb_logged_in and not force:
        return
    _bb_login()


def _bb_call(module, method, params=()):
    """Вызов серверного метода bugboard (G5 module/call)."""
    _bb_ensure()
    body = {"moduleName": module, "methodName": method,
            "parameters": [{"type": t, "value": v} for t, v in params],
            "remoteCallerInfo": {"id": str(uuid.uuid4()),
                                 "debugState": None, "bslStack": []}}
    resp = _post(f"{BB_BASE}/ui/module/call", json=body,
                 headers={"X-G5-Version": _bb_ver})
    if resp.status_code in (401, 403):
        _bb_login()  # сессия истекла — перезаход и повтор
        resp = _post(f"{BB_BASE}/ui/module/call", json=body,
                     headers={"X-G5-Version": _bb_ver})
    if resp.status_code != 200:
        raise RuntimeError(f"bugboard/{method}: HTTP {resp.status_code} — "
                           "протокол мог измениться, см. docs/bugboard-protocol.md")
    return resp.json().get("result", {})


def _bb_read(ref_type, ref_value):
    """Карточка объекта bugboard (entity/read)."""
    body = {"reference": {"type": ref_type, "value": ref_value}}
    resp = _post(f"{BB_BASE}/ui/entity/read", json=body,
                 headers={"X-G5-Version": _bb_ver})
    if resp.status_code != 200:
        raise RuntimeError(f"bugboard entity/read: HTTP {resp.status_code}")
    return resp.json().get("object", {}).get("value", {})


def _bb_status_name(typed):
    """Укрупнённый статус ошибки: число -> представление."""
    try:
        i = int(typed.get("value"))
        return BB_STATUS[i] if 0 <= i < len(BB_STATUS) else str(i)
    except (TypeError, ValueError):
        return "не указан"


def _bb_undef(result):
    return not result or result.get("type") == "Std::Undefined"


def _bb_find_project(code):
    res = _bb_call(BB_ROUTING, "НайтиПроектПоКоду", [("Std::String", code)])
    if _bb_undef(res):
        raise RuntimeError(f"Проект «{code}» на bugboard не найден")
    return res["value"]


def _bb_card_md(card):
    st = _bb_status_name(card.get("УкрупненныйСтатус", {}))
    lines = [f"# {card.get('Name', '?')} — {card.get('Заголовок', '')}",
             f"Статус: {st} | Опубликовано: {card.get('ДатаПубликации', '?')}",
             f"Источник: https://bugboard.1c.ru/?state={card.get('ФрагментСсылки', '')}"]
    if card.get("ВерсииИсправленияПредставление"):
        lines.append(f"Продукт: {card['ВерсииИсправленияПредставление']}")
    if card.get("ПланируемаяДатаИсправления", "") not in ("", "0001-01-01"):
        lines.append(f"Планируемая дата исправления: "
                     f"{card['ПланируемаяДатаИсправления']}")
    if card.get("КодОбращения"):
        lines.append(f"Обращение: {card['КодОбращения']}")
    for title, field in (("Описание", "Описание"),
                         ("Способ обхода", "СпособОбхода"),
                         ("Способ исправления", "СпособИсправления")):
        if card.get(field):
            lines.append(f"\n**{title}.** {card[field]}")
    return "\n".join(lines)


def bugboard_card_raw(number, project=""):
    s = number.strip()
    m = re.search(r"state=([\w-]+)", s)
    if m:
        state = m.group(1)
    elif s.startswith("prj-"):
        state = s
    else:
        num = re.sub(r"(?i)^EF_", "", s).replace("_", "-")
        if not re.match(r"^[\d-]+$", num):
            return ("Укажите номер вида EF_00_00928768 (или 00-00928768, "
                    "60033691) и код проекта, либо полную ссылку/адрес страницы.")
        if project:
            state = f"prj-{project}-er-{num}"
        else:
            # глобальный поиск по номеру (bugboard ищет только по номеру,
            # тексты/симптомы портал не индексирует)
            res = _bb_call("e1c::bugboard::Компоненты::ПоискДанных::ПоискОшибок",
                           "ВыполнитьПоискПоОшибкам", [("Std::String", num)])
            items = (res.get("value", {}).get("НайденныеОшибки", {})
                     .get("value", {}).get("items", []))
            if len(items) == 1:
                return _bb_card_md(_bb_read(BB_REF_ERROR, items[0]["value"]))
            if not items:
                return (f"Ошибка с номером {num} на bugboard не найдена. "
                        "Проверьте номер; по симптомам bugboard не ищет — "
                        "используйте releases_patches (там тексты исправлений).")
            cards = [_bb_read(BB_REF_ERROR, it["value"]) for it in items[:5]]
            listing = "\n".join(f"- {c.get('Name')}: {c.get('Заголовок', '')}"
                                for c in cards)
            return (f"Номер {num} нашёлся в {len(items)} проектах:\n{listing}\n"
                    "Уточните код проекта вторым аргументом.")
    res = _bb_call(BB_ROUTING, "ПолучитьОшибкуПоФрагментуСсылкиИзURL",
                   [("Std::String", state)])
    if _bb_undef(res):
        return (f"Ошибка «{state}» на bugboard не найдена. Проверьте номер, "
                "код проекта и что ошибка вообще опубликована на bugboard.")
    return _bb_card_md(_bb_read(BB_REF_ERROR, res["value"]))


def _bb_export_cards(project, ver):
    """Все карточки ошибок версии одной выгрузкой (bugboard, JSON)."""
    key = f"bbexport:{project}:{ver}"
    cached = _cache_get(key, CACHE_TTL)
    if cached is not None:
        return json.loads(cached)
    proj = _bb_find_project(project)
    vres = _bb_call(BB_ROUTING, "ПолучитьВерсиюПоНаименованиюИПроекту",
                    [("Std::String", ver), (BB_REF_PROJECT, proj)])
    if _bb_undef(vres):
        raise RuntimeError(f"Версия {ver} проекта {project} на bugboard не найдена")
    res = _bb_call(BB_VERSIONS, "ПолучитьМассивОшибокВВерсии",
                   [(BB_REF_PROJECT, proj), (BB_REF_VERSION, vres["value"])])
    val = res.get("value", {})
    items = (val.get("МассивИсправленныхОшибокВВерсии", {})
             .get("value", {}).get("items", [])
             + val.get("МассивНеисправленныхОшибокВВерсии", {})
             .get("value", {}).get("items", []))
    if not items:
        raise RuntimeError(f"У версии {ver} проекта {project} на bugboard "
                           "ошибок не найдено")
    arr = "Std::Collections::Array<e1c::bugboard::Багборд::Ошибки.Reference>"
    export = _bb_call("e1c::bugboard::ВыгрузкаОшибок::ВыгрузкаОшибокВФайл",
                      "ВыгрузитьОшибкиВВерсииВJSON",
                      [(arr, {"items": items}),
                       (BB_REF_VERSION, vres["value"]),
                       (BB_REF_PROJECT, proj)])
    binref = export.get("value")
    if not binref:
        raise RuntimeError("bugboard: выгрузка не вернула файл")
    r = _get(f"{BB_BASE}/sys/binary/{binref}", headers={"X-G5-Version": _bb_ver})
    if r.status_code != 200:
        raise RuntimeError(f"bugboard: скачивание выгрузки — HTTP "
                           f"{r.status_code}")
    cards = json.loads(r.text)
    _cache_put(key, json.dumps(cards, ensure_ascii=False))
    return cards


def _bb_export_status(card):
    st = card.get("Статус")
    if isinstance(st, dict):
        return _bb_status_name(st)
    text = str(st or "не указан")
    return re.sub(r"(?<=[а-яё])(?=[А-ЯЁ])", " ", text)


def bugboard_search_raw(project, ver, query, limit=20):
    """Поиск по симптомам: подстрока в заголовке/описании ошибок версии."""
    limit = max(1, min(int(limit), 50))
    cards = _bb_export_cards(project, ver)
    q = (query or "").strip().lower()
    hits = [c for c in cards
            if q in " ".join(str(c.get(k, "")) for k in
                             ("Заголовок", "Описание", "СпособОбхода",
                              "КодОшибки")).lower()]
    lines = [f"Поиск «{query}» среди ошибок версии {ver} проекта {project} "
             f"(bugboard): найдено {len(hits)} из {len(cards)}"]
    for c in hits[:limit]:
        lines.append(f"- {c.get('КодОшибки', '?')}: {c.get('Заголовок', '')} — "
                     f"{_bb_export_status(c)}")
    if len(hits) > limit:
        lines.append(f"… и ещё {len(hits) - limit}")
    if not hits:
        lines.append("Ничего не найдено. Попробуйте другие слова или соседнюю "
                     "версию (bugboard_versions).")
    return "\n".join(lines)


def bugboard_versions_raw(project, count=10):
    count = max(1, min(int(count), 20))
    key = f"bbversions:{project}:{count}"
    cached = _cache_get(key, SEARCH_TTL)
    if cached:
        return cached
    proj = _bb_find_project(project)
    res = _bb_call(BB_VERSIONS, "ПолучитьПревьюВерсийПроекта",
                   [(BB_REF_PROJECT, proj), ("Std::Number", count)])
    items = res.get("value", {}).get("items", [])
    out = []
    for it in items:
        card = _bb_read(BB_REF_VERSION, it.get("value"))
        name = (card.get("Наименование") or card.get("Name")
                or card.get("Presentation") or str(it.get("value", ""))[:8])
        out.append(f"- {name}")
    if not out:
        return f"Версии проекта {project} не получены."
    result = f"Последние версии проекта {project} на bugboard:\n" + "\n".join(out)
    _cache_put(key, result)
    return result


def bugboard_version_errors_raw(project, ver, limit=10):
    limit = max(1, min(int(limit), 30))
    key = f"bberrors:{project}:{ver}:{limit}"
    cached = _cache_get(key, SEARCH_TTL)
    if cached:
        return cached
    proj = _bb_find_project(project)
    vres = _bb_call(BB_ROUTING, "ПолучитьВерсиюПоНаименованиюИПроекту",
                    [("Std::String", ver), (BB_REF_PROJECT, proj)])
    if _bb_undef(vres):
        return f"Версия {ver} проекта {project} на bugboard не найдена."
    res = _bb_call(BB_VERSIONS, "ПолучитьМассивОшибокВВерсии",
                   [(BB_REF_PROJECT, proj), (BB_REF_VERSION, vres["value"])])
    val = res.get("value", {})
    groups = (("Исправленные в этой версии", "МассивИсправленныхОшибокВВерсии"),
              ("Не исправленные (проявляются)", "МассивНеисправленныхОшибокВВерсии"))
    lines = [f"Проект {project}, версия {ver} (bugboard):"]
    for label, field in groups:
        items = val.get(field, {}).get("value", {}).get("items", [])
        lines.append(f"\n## {label}: {len(items)}")
        for it in items[:limit]:
            card = _bb_read(BB_REF_ERROR, it.get("value"))
            st = _bb_status_name(card.get("УкрупненныйСтатус", {}))
            lines.append(f"- {card.get('Name', '?')}: "
                         f"{card.get('Заголовок', '')} — {st}")
        if len(items) > limit:
            lines.append(f"… и ещё {len(items) - limit}; полные карточки — "
                         "bugboard_card по номеру")
    result = "\n".join(lines)
    _cache_put(key, result)
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


@mcp.tool()
def releases_products(query: str = "") -> str:
    """Продукты и типовые конфигурации с releases.1c.ru: название, ник
    (для releases_patches) и актуальные версии. query — подстрока для
    фильтра, например «Бухгалтерия», «Зарплата», «ERP»; пусто — все."""
    return _safe(products_raw, query)


@mcp.tool()
def releases_history(nick: str, limit: int = 15) -> str:
    """История релизов конфигурации с releases.1c.ru: номер версии, дата
    выхода, с каких версий возможно обновление, минимальная версия
    платформы. nick — код продукта из releases_products
    (например Accounting30); свежие версии сверху."""
    return _safe(history_raw, nick, limit)


@mcp.tool()
def releases_where_fixed(nick: str, number: str, depth: int = 8) -> str:
    """В каком релизе исправление вошло в состав самой конфигурации, а в
    каких доступно только багфиксом. nick — код продукта
    (releases_products), number — номер исправления EF_... (или без
    префикса). depth — сколько последних релизов просмотреть (2-15);
    списки исправлений кэшируются, повторные вызовы быстрые."""
    return _safe(where_fixed_raw, nick, number, depth)


@mcp.tool()
def releases_patches(nick: str, ver: str) -> str:
    """Список исправлений (баг-фиксов EF_...) конкретной версии типовой
    конфигурации с releases.1c.ru. nick и ver — из результата
    releases_products, например nick=Accounting30, ver=3.0.203.24.
    Номер EF_... можно искать на bugboard.1c.ru вручную."""
    return _safe(patches_raw, nick, ver)


@mcp.tool()
def bugboard_card(number: str, project: str = "") -> str:
    """Карточка ошибки с bugboard.1c.ru (1С:Публикация ошибок): заголовок,
    описание, статус, способ обхода, план исправления. number — номер
    вида EF_00_00928768 (или 00-00928768) вместе с кодом проекта
    (project, например ssl22 или bp3), либо полная строка состояния
    (prj-ssl22-er-00-00928768) или URL страницы ошибки."""
    return _safe(bugboard_card_raw, number, project)


@mcp.tool()
def bugboard_version_errors(project: str, ver: str, limit: int = 10) -> str:
    """Список ошибок версии конфигурации с bugboard.1c.ru: исправленные
    и неисправленные, с заголовками и статусами. project — код проекта
    bugboard (например bp3, ssl22), ver — номер версии (3.0.203.24).
    limit — сколько карточек читать в каждой группе (1-30; каждая
    карточка — запрос к порталу, по умолчанию 10)."""
    return _safe(bugboard_version_errors_raw, project, ver, limit)


@mcp.tool()
def bugboard_search(project: str, ver: str, query: str, limit: int = 20) -> str:
    """Поиск ошибок по симптомам: подстрока в заголовке, описании или
    способе обхода всех ошибок версии конфигурации на bugboard.1c.ru.
    project — код проекта bugboard (bp3, ssl22), ver — версия (3.0.205.22),
    query — текст симптома (например «ДиректБанк» или «суточных»).
    Скачивает полный список ошибок версии одной выгрузкой (кэш на неделю),
    поэтому повторные запросы быстрые."""
    return _safe(bugboard_search_raw, project, ver, query, limit)


@mcp.tool()
def bugboard_versions(project: str, count: int = 10) -> str:
    """Последние версии проекта на bugboard.1c.ru. project — код проекта
    (например bp3, ssl22)."""
    return _safe(bugboard_versions_raw, project, count)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
