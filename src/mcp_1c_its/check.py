# -*- coding: utf-8 -*-
"""Проверка сервера ИТС. Запуск после установки пакета:

    python -m mcp_1c_its.check

Проверяет все три ресурса: ИТС (вход, поиск, материал), releases.1c.ru
(конфигурации) и bugboard.1c.ru (вход и версии проекта). Логин и пароль
печатаются только в виде «задан/не задан». Код возврата 1 — если хотя
бы одна часть не работает.
"""
from .server import (ITS_PASS, ITS_USER, _CRED_SOURCE, _ensure_session,
                     bugboard_versions_raw, get_raw, products_raw, search_raw)


def main():
    ok = True

    print("=== 1С:ИТС ===")
    print("Источник учётных данных:", _CRED_SOURCE)
    print("ITS_USER задан:", bool(ITS_USER))
    print("ITS_PASS задан:", bool(ITS_PASS))
    if not (ITS_USER and ITS_PASS):
        raise SystemExit(1)
    try:
        _ensure_session()
        print("Вход в ИТС: успешен")
        print("Пробный поиск (v8std):",
              search_raw("временные таблицы", "v8std").splitlines()[1][:90])
        print("Пробный материал (/db/v8std/content/777):",
              get_raw("/db/v8std/content/777").splitlines()[0][:90])
    except Exception as exc:
        print("Ошибка ИТС:", exc)
        ok = False

    print()
    print("=== releases.1c.ru ===")
    try:
        lines = products_raw("Бухгалтерия").splitlines()
        print("Конфигурации найдены:", lines[0][:90])
        print("Пример:", lines[1][:90] if len(lines) > 1 else "-")
    except Exception as exc:
        print("Ошибка releases:", exc)
        ok = False

    print()
    print("=== bugboard.1c.ru ===")
    try:
        lines = bugboard_versions_raw("bp3", 3).splitlines()
        print("Вход и версии проекта bp3:", lines[1][:90]
              if len(lines) > 1 else lines[0][:90])
    except Exception as exc:
        print("Ошибка bugboard:", exc)
        ok = False

    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
