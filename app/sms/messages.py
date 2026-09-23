"""SMS reply copy for the bed-update flow (BB-5 / BB-10): confirmation,
help/usage, and unknown-number rejection text, in English, Spanish and
Vietnamese.

Assumption: Vietnamese copy is written without diacritics (ASCII-only),
matching the token design in parser.py, since SMS transport may not
render Vietnamese diacritics reliably on every handset/carrier.
"""

from __future__ import annotations

from app.schemas import Locale, SmsPopulationType

_POPULATION_ORDER: tuple[SmsPopulationType, ...] = (
    SmsPopulationType.WOMEN,
    SmsPopulationType.MEN,
    SmsPopulationType.FAMILY,
)

# The word used to label each bucket in reply copy, per locale. These
# intentionally match the primary word token in parser.py's tables.
_POPULATION_LABELS: dict[Locale, dict[SmsPopulationType, str]] = {
    Locale.EN: {
        SmsPopulationType.WOMEN: "women",
        SmsPopulationType.MEN: "men",
        SmsPopulationType.FAMILY: "family",
    },
    Locale.ES: {
        SmsPopulationType.WOMEN: "mujeres",
        SmsPopulationType.MEN: "hombres",
        SmsPopulationType.FAMILY: "familias",
    },
    Locale.VI: {
        SmsPopulationType.WOMEN: "nu",
        SmsPopulationType.MEN: "nam",
        SmsPopulationType.FAMILY: "gia dinh",
    },
}

_CONFIRMATION_PREFIX: dict[Locale, str] = {
    Locale.EN: "Saved",
    Locale.ES: "Guardado",
    Locale.VI: "Da luu",
}

_HELP_TEXT: dict[Locale, str] = {
    Locale.EN: (
        "BedBoard: text your open bed counts like 'W 3 M 1 F 0' "
        "(women, men, family), or update just one bucket, e.g. "
        "'women 3' or 'men 2'."
    ),
    Locale.ES: (
        "BedBoard: envia tus camas disponibles asi 'M 3 H 1 F 0' "
        "(mujeres, hombres, familias), o actualiza un solo grupo, ej. "
        "'mujeres 3' o 'hombres 2'."
    ),
    Locale.VI: (
        "BedBoard: nhan tin so giuong trong theo mau 'N 3 D 1 G 0' "
        "(nu, nam, gia dinh), hoac chi cap nhat mot muc, vd. 'nu 3' "
        "hoac 'nam 2'."
    ),
}

_REJECTED_UNKNOWN_NUMBER_TEXT: dict[Locale, str] = {
    Locale.EN: (
        "BedBoard: this number isn't registered as a shelter "
        "coordinator, so your update wasn't saved. Contact your "
        "BedBoard admin to get set up."
    ),
    Locale.ES: (
        "BedBoard: este numero no esta registrado como coordinador de "
        "refugio, asi que tu actualizacion no se guardo. Contacta a tu "
        "administrador de BedBoard para registrarte."
    ),
    Locale.VI: (
        "BedBoard: so nay chua duoc dang ky la dieu phoi vien noi tru, "
        "nen cap nhat cua ban chua duoc luu. Hay lien he quan tri vien "
        "BedBoard de duoc dang ky."
    ),
}


def render_confirmation(counts: dict[SmsPopulationType, int], locale: Locale) -> str:
    """Render the reply sent after a bed-count update was saved.

    Only includes the buckets present in `counts` (partial-update
    semantics -- see parser.py's ParsedSmsUpdate.counts), always in a
    fixed women/men/family order regardless of dict insertion order, e.g.
    "Saved: 3 women, 1 men, 0 family" or just "Saved: 3 women" for a
    single-bucket update.
    """
    prefix = _CONFIRMATION_PREFIX.get(locale, _CONFIRMATION_PREFIX[Locale.EN])
    labels = _POPULATION_LABELS.get(locale, _POPULATION_LABELS[Locale.EN])
    parts = [
        f"{counts[pop_type]} {labels[pop_type]}"
        for pop_type in _POPULATION_ORDER
        if pop_type in counts
    ]
    return f"{prefix}: {', '.join(parts)}"


def render_help(locale: Locale) -> str:
    """Render the usage/help reply shown when parsing an inbound SMS fails."""
    return _HELP_TEXT.get(locale, _HELP_TEXT[Locale.EN])


def render_rejected_unknown_number(locale: Locale = Locale.EN) -> str:
    """Render the reply for an inbound SMS from a phone number that isn't a
    registered coordinator.

    The caller passes Locale.EN for this one, since the sender's locale is
    unknown by definition when their number isn't registered -- but the
    function itself supports all locales in case it's ever invoked with a
    known locale.
    """
    return _REJECTED_UNKNOWN_NUMBER_TEXT.get(locale, _REJECTED_UNKNOWN_NUMBER_TEXT[Locale.EN])
