"""SMS copy for the stale-count nudge job (BB-6).

Two message levels:
  - render_nudge: level-1, sent to the shelter's active coordinators when a
    site's bed count first goes STALE.
  - render_escalation: level-2, sent to "lead"-shift coordinators when a
    level-1 nudge has gone unresolved for nudge_escalation_minutes. Reads
    more urgent than the level-1 copy and calls out that the coordinator
    has not responded.

Kept independent of app/sms/messages.py (a separate module, built
concurrently) even though both speak English/Spanish/Vietnamese -- no
shared import, per the ground rules.
"""

from __future__ import annotations

from app.schemas import Locale

_NUDGE_TEMPLATES: dict[Locale, str] = {
    Locale.EN: (
        "BedBoard: {shelter_name}'s bed count hasn't been updated in over "
        "8 hours. Please reply with your current counts (e.g. W 3 M 1 F 0) "
        "as soon as possible."
    ),
    Locale.ES: (
        "BedBoard: El conteo de camas de {shelter_name} no se ha "
        "actualizado en más de 8 horas. Por favor responda con sus "
        "conteos actuales (ej. W 3 M 1 F 0) lo antes posible."
    ),
    Locale.VI: (
        "BedBoard: Số giường của {shelter_name} chưa được cập nhật trong "
        "hơn 8 giờ. Vui lòng trả lời với số liệu hiện tại (vd: W 3 M 1 F 0) "
        "càng sớm càng tốt."
    ),
}

_ESCALATION_TEMPLATES: dict[Locale, str] = {
    Locale.EN: (
        "BedBoard URGENT: {shelter_name} still has no updated bed count "
        "and the on-shift coordinator has not responded in over 30 "
        "minutes. Please follow up now or send an updated count."
    ),
    Locale.ES: (
        "BedBoard URGENTE: {shelter_name} todavía no tiene un conteo de "
        "camas actualizado y el coordinador de turno no ha respondido en "
        "más de 30 minutos. Por favor haga seguimiento ahora o envíe un "
        "conteo actualizado."
    ),
    Locale.VI: (
        "BedBoard KHẨN CẤP: {shelter_name} vẫn chưa có số giường cập nhật "
        "và điều phối viên trực ca chưa phản hồi sau hơn 30 phút. Vui lòng "
        "liên hệ ngay hoặc gửi số liệu cập nhật."
    ),
}


def render_nudge(shelter_name: str, locale: Locale) -> str:
    """Level-1 stale-count nudge, addressed to the shelter's coordinator(s)."""
    template = _NUDGE_TEMPLATES.get(locale, _NUDGE_TEMPLATES[Locale.EN])
    return template.format(shelter_name=shelter_name)


def render_escalation(shelter_name: str, locale: Locale) -> str:
    """Level-2 escalation, addressed to the shelter's site lead(s)."""
    template = _ESCALATION_TEMPLATES.get(locale, _ESCALATION_TEMPLATES[Locale.EN])
    return template.format(shelter_name=shelter_name)
