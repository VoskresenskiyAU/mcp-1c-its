# -*- coding: utf-8 -*-
"""Проверка сервера ИТС. Запуск после установки пакета:

    python -m mcp_1c_its.check

Проверяет все четыре ресурса: ИТС (вход, поиск, материал), releases.1c.ru
(конфигурации), bugboard.1c.ru (вход и версии проекта) и buhexpert8.ru
(поиск, материал; анонимно или по учётной записи подписки). Логин и пароль
печатаются только в виде «задан/не задан». Код возврата 1 — если хотя
бы одна часть не работает.
"""
import re

from .server import (BE_USER, ITS_PASS, ITS_USER, _CRED_SOURCE, _be_cred_source,
                     _be_ensure_session, _be_state, _ensure_session,
                     be_get_raw, be_search_raw, bugboard_versions_raw,
                     get_raw, products_raw, search_raw)


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

    print()
    print("=== buhexpert8.ru ===")
    print("Учётные данные подписки:", _be_cred_source)
    print("BUHEXPERT_USER задан:", bool(BE_USER))
    try:
        found = be_search_raw("суточные", 3).splitlines()
        print("Поиск:", found[0][:90])
        m = re.search(r"\(id (\d+)\)",
                      found[2] if len(found) > 2 else "")
        if m:
            got = be_get_raw(m.group(1))
            print(f"Материал (id {m.group(1)}):",
                  got.splitlines()[0][:90])
        else:
            print("Материал: в выдаче нет строк с id")
        if BE_USER:
            _be_ensure_session()
            print("Вход:", "выполнен (доступ подписчика)" if _be_state["ok"]
                  else _be_state["note"])
    except Exception as exc:
        print("Ошибка buhexpert8:", exc)
        ok = False

    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
