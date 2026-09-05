# -*- coding: utf-8 -*-
"""Проверка сервера ИТС. Запуск после установки пакета:

    python -m mcp_1c_its.check

Логин и пароль печатаются только в виде «задан/не задан».
"""
from .server import (ITS_PASS, ITS_USER, _CRED_SOURCE, _ensure_session,
                     get_raw, search_raw)


def main():
    print("Источник учётных данных:", _CRED_SOURCE)
    print("ITS_USER задан:", bool(ITS_USER))
    print("ITS_PASS задан:", bool(ITS_PASS))
    if not (ITS_USER and ITS_PASS):
        raise SystemExit(1)
    try:
        _ensure_session()
        print("Вход в ИТС: успешен")
    except Exception as exc:
        print("Вход в ИТС: ошибка —", exc)
        raise SystemExit(1)
    print("\nПробный поиск (раздел v8std):")
    print(search_raw("временные таблицы", "v8std")[:700])
    print("\nПробный материал (/db/v8std/content/777):")
    print(get_raw("/db/v8std/content/777")[:500])


if __name__ == "__main__":
    main()
