"""Public import diagnostics, without Moodle bodies, file names or session data."""
from __future__ import annotations

import re

from app.models.integration import SyncOutbox

WARNING_MESSAGES = {
    "ARCHIVE_CHECKSUM_MISMATCH": (
        "При распаковке вложения не совпала контрольная сумма архива. "
        "Нужна исправная копия файла в Moodle; повторная синхронизация того же файла не поможет."
    ),
    "FILE_TOO_LARGE": "Некоторые вложения превышают лимит 100 МБ на файл.",
    "DOWNLOAD_TIMEOUT": "Не удалось скачать отдельные вложения за отведённое время.",
    "ARCHIVE_SOURCE_OMITTED": (
        "Некоторые архивы не удалось распаковать или прочитать в пределах безопасных ограничений."
    ),
    "DETAIL_NAVIGATION_FAILED": "Не удалось открыть отдельные ответы в Moodle.",
    "DETAIL_MARKUP_UNSUPPORTED": "Не распознан формат страницы отдельного ответа Moodle.",
    "DETAIL_PAGINATION_INCOMPLETE": "Загружены не все страницы отдельных ответов Moodle.",
    "ARTIFACT_OMITTED": "Часть файлов ответов не удалось скачать из Moodle.",
    "ANSWER_OMITTED": "Часть текста ответов превышает допустимый размер загрузки.",
    "FIELD_OMITTED": "Часть условий или комментариев превышает допустимый размер загрузки.",
    "SKIPPED_UNIDENTIFIED_ROWS": "Не удалось определить студентов у части строк отчёта Moodle.",
    "DELETION_CHECK_INCOMPLETE": (
        "Не удалось проверить удаление некоторых попыток в Moodle. Локальные данные сохранены."
    ),
    "ACTIVITY_METADATA_INCOMPLETE": "Не удалось уточнить часть параметров работы в Moodle.",
    "ATTEMPT_IN_PROGRESS": "Некоторые попытки в Moodle ещё не завершены.",
    "HISTORY_IMPORT_WARNING": "При загрузке отдельных данных Moodle возникли предупреждения.",
}
ERROR_MESSAGES = {
    "LMS_REAUTH_REQUIRED": "Истекла сессия преподавателя в Moodle. Войдите повторно.",
    "CREDENTIAL_EXPIRED": "Истекла сессия преподавателя в Moodle. Войдите повторно.",
    "MISSING_CREDENTIAL": "Не найдена сессия преподавателя Moodle. Войдите повторно.",
    "TIMEOUT": "Moodle не ответил вовремя. Повторите синхронизацию работы.",
    "UNAVAILABLE": "Соединение с Moodle недоступно. Повторите синхронизацию позже.",
    "BROWSER_BUSY": "Очередь чтения Moodle занята. Повторите синхронизацию позже.",
    "INVALID_RESPONSE": "Не удалось распознать ответ Moodle. Сообщите администратору.",
    "RESPONSE_TOO_LARGE": "Ответ Moodle превышает допустимый размер загрузки.",
    "TEACHER_MEMBERSHIP_REQUIRED": "Moodle не подтвердил права преподавателя в этом курсе.",
    "NOT_CONFIGURED": "Подключение к Moodle не настроено. Сообщите администратору.",
}


def history_warning_codes(warnings: object) -> list[str]:
    """Keep bounded, known categories; never persist the identifier/text suffix."""
    if not isinstance(warnings, list):
        return []
    codes = {
        code if code in WARNING_MESSAGES else "HISTORY_IMPORT_WARNING"
        for warning in warnings[:32]
        for code in [warning.partition(":")[0] if isinstance(warning, str) else ""]
    }
    return sorted(codes)


def history_import_diagnostic(rows: list[SyncOutbox]) -> tuple[str, str]:
    for row in rows:
        if row.state not in {"FAILED", "BLOCKED"}:
            continue
        raw = row.last_error or ""
        code = raw.partition(":")[0]
        if code == "INVALID_RESPONSE" and re.match(
            r"^INVALID_RESPONSE: (ASSIGN|QUIZ)_TABLE_NOT_FOUND:", raw
        ):
            return "SUBMISSIONS_TABLE_NOT_FOUND", (
                "Не распознана таблица сдач Moodle. Это не означает отсутствие работ; "
                "сообщите администратору."
            )
        if code in ERROR_MESSAGES:
            return code, ERROR_MESSAGES[code]
        if re.fullmatch(r"UNEXPECTED_[A-Z_]{1,48}", code):
            return code, "Внутренняя ошибка обработки импорта. Сообщите администратору."
        return "HISTORY_IMPORT_FAILED", (
            "Не удалось загрузить все ответы из Moodle. Повторите синхронизацию работы."
        )
    codes = {
        code
        for row in rows
        for code in history_warning_codes((row.receipt or {}).get("warning_codes", []))
    }
    # Prefer missing answer content over ancillary metadata/deletion warnings.
    for code, message in WARNING_MESSAGES.items():
        if code in codes:
            return code, message
    # Previous receipts kept only warning_count. Do not invent its cause.
    return "HISTORY_IMPORT_WARNING", WARNING_MESSAGES["HISTORY_IMPORT_WARNING"]
