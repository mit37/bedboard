"""SMS bed-update parser (BB-5) and locale-aware token tables (BB-10).

Assumption -- partial-update semantics: a coordinator can text a single
bucket (e.g. "women 3") to update just that population count without
resetting the other two buckets. ``ParsedSmsUpdate.counts`` therefore only
ever contains the population buckets that were explicitly present in the
message text; a bucket that wasn't mentioned is simply absent from the
dict (never defaulted to 0). This is a deliberate product decision, not a
gap: BB-5's example ("W 3 M 1 F 0") happens to update all three buckets at
once, but nothing in the PRD says every update must be a full snapshot,
and requiring a coordinator to re-type all three numbers every time they
only want to correct one bucket would be needless friction during a shift
change. Freshness/DB-write/registration concerns are explicitly out of
scope for this module (handled later in app/sms/webhook.py).

Assumption -- locale token tables (BB-10 doesn't specify exact tokens, so
these are this module's own choice; documented here for audit and easy
future changes):

    English (en): W=women, M=men, F=family (letter codes); "women"/
        "woman"/"men"/"man"/"family"/"families" (word forms).
    Spanish (es): M=Mujeres (women), H=Hombres (men), F=Familias (family).
        The three letters are distinct from each other (M/H/F) even
        though, confusingly, "M" means something different than it does
        in English -- this is safe because the parser only ever compares
        a message against one locale's table at a time (see "Locale
        fallback" below), never several tables simultaneously.
    Vietnamese (vi): N=Nu (women, from "Nu"), D=Dan ong (men, formal
        "man"), G=Gia dinh (family). All Vietnamese tokens are written
        ASCII-only (no diacritics) since SMS transport may not render
        Vietnamese diacritics reliably on every handset/carrier. Note the
        common colloquial word for "men", "nam", also starts with the
        same letter as "Nu" (women) -- it's accepted as a *word* token
        below, but deliberately NOT used as a letter code, so the three
        letter codes (N/D/G) stay unambiguous.

Locale fallback: the caller already knows the coordinator's registered
locale (CoordinatorPhoneModel.locale) and passes it in. ``parse_sms``
tries that locale's token table first; if (and only if) it finds zero
buckets there, it retries the *whole* message against the English token
table before giving up, since coordinators sometimes text in English even
when their profile is set to es/vi. It does not try every other locale's
table -- a message that uses, say, Vietnamese tokens while the
coordinator is registered as "en" is treated as unrecognized, matching
the PRD's "wrong locale keywords -> error" case. The ``locale`` field on a
successful ``ParsedSmsUpdate`` always reflects the caller-supplied locale
(the coordinator's registered reply language), not which token table
happened to match -- that's what messages.py uses to pick the reply
language.
"""

from __future__ import annotations

import re

from app.schemas import Locale, ParsedSmsUpdate, SmsParseError, SmsPopulationType

MAX_COUNT = 500

# --- Token tables -------------------------------------------------------
# Each locale maps every SmsPopulationType to the literal single-letter
# codes and word/phrase forms a coordinator may use for that bucket. All
# entries are lowercase; matching is done against a lowercased copy of the
# inbound text.

_EN_TOKENS: dict[SmsPopulationType, dict[str, list[str]]] = {
    SmsPopulationType.WOMEN: {"letters": ["w"], "words": ["women", "woman"]},
    SmsPopulationType.MEN: {"letters": ["m"], "words": ["men", "man"]},
    SmsPopulationType.FAMILY: {"letters": ["f"], "words": ["family", "families"]},
}

_ES_TOKENS: dict[SmsPopulationType, dict[str, list[str]]] = {
    SmsPopulationType.WOMEN: {"letters": ["m"], "words": ["mujeres", "mujer"]},
    SmsPopulationType.MEN: {"letters": ["h"], "words": ["hombres", "hombre"]},
    SmsPopulationType.FAMILY: {"letters": ["f"], "words": ["familias", "familia"]},
}

_VI_TOKENS: dict[SmsPopulationType, dict[str, list[str]]] = {
    SmsPopulationType.WOMEN: {"letters": ["n"], "words": ["nu", "phu nu"]},
    SmsPopulationType.MEN: {"letters": ["d"], "words": ["nam", "dan ong"]},
    SmsPopulationType.FAMILY: {"letters": ["g"], "words": ["gia dinh", "giadinh"]},
}

_TOKEN_TABLES: dict[Locale, dict[SmsPopulationType, dict[str, list[str]]]] = {
    Locale.EN: _EN_TOKENS,
    Locale.ES: _ES_TOKENS,
    Locale.VI: _VI_TOKENS,
}


def _word_alt(word: str) -> str:
    """Turn a token word/phrase into a regex alternative that tolerates
    flexible inter-word whitespace (so "gia dinh" also matches
    "gia   dinh")."""
    parts = [re.escape(p) for p in word.split()]
    return r"\s+".join(parts)


def _build_patterns(
    table: dict[SmsPopulationType, dict[str, list[str]]],
) -> dict[SmsPopulationType, re.Pattern[str]]:
    """Compile one regex per population type: word forms (word-bounded) or
    a letter code (not preceded by another letter, so it can't match
    mid-word), followed by optional whitespace and a signed integer. The
    number is captured with an optional leading '-' and up to 4 digits so
    negative counts and over-range counts can still be matched and then
    explicitly rejected by the caller, instead of silently failing to
    match at all.
    """
    patterns: dict[SmsPopulationType, re.Pattern[str]] = {}
    for pop_type, tokens in table.items():
        word_alts = sorted((_word_alt(w) for w in tokens["words"]), key=len, reverse=True)
        word_group = r"\b(?:" + "|".join(word_alts) + r")\b" if word_alts else ""
        letter_group = ""
        if tokens["letters"]:
            letters = "".join(re.escape(letter) for letter in tokens["letters"])
            letter_group = r"(?<![a-z])[" + letters + r"]"
        alts = [g for g in (word_group, letter_group) if g]
        combined = "(?:" + "|".join(alts) + r")\s*(-?\d{1,4})"
        patterns[pop_type] = re.compile(combined, re.IGNORECASE)
    return patterns


_COMPILED_TABLES: dict[Locale, dict[SmsPopulationType, re.Pattern[str]]] = {
    locale: _build_patterns(table) for locale, table in _TOKEN_TABLES.items()
}


def _match_table(
    text: str, patterns: dict[SmsPopulationType, re.Pattern[str]]
) -> tuple[dict[SmsPopulationType, int], str | None]:
    """Run every population's pattern against `text` independently (so a
    message can freely mix letter-code and word-form buckets). Returns
    (counts_found_so_far, error_reason). error_reason is set the moment an
    out-of-range or negative value is found, at which point the caller
    should treat the whole message as an error and discard partial
    counts.
    """
    counts: dict[SmsPopulationType, int] = {}
    for pop_type, pattern in patterns.items():
        match = pattern.search(text)
        if not match:
            continue
        value = int(match.group(1))
        if value < 0:
            return counts, "negative_count"
        if value > MAX_COUNT:
            return counts, "out_of_range"
        counts[pop_type] = value
    return counts, None


def parse_sms(raw_text: str, locale: Locale) -> ParsedSmsUpdate | SmsParseError:
    """Parse a coordinator's inbound bed-count SMS (BB-5 / BB-10).

    Tries `locale`'s token table first; if it finds no buckets at all,
    falls back to the English table (coordinators sometimes text in
    English regardless of their registered locale) -- see module
    docstring "Locale fallback". Never raises: any empty, unrecognized,
    negative, or out-of-range input comes back as an ``SmsParseError``
    instead.
    """
    if not raw_text or not raw_text.strip():
        return SmsParseError(reason="empty", raw_text=raw_text or "")

    text = raw_text.strip().lower()

    primary_patterns = _COMPILED_TABLES[locale]
    counts, error_reason = _match_table(text, primary_patterns)
    if error_reason is not None:
        return SmsParseError(reason=error_reason, raw_text=raw_text)

    if not counts and locale != Locale.EN:
        counts, error_reason = _match_table(text, _COMPILED_TABLES[Locale.EN])
        if error_reason is not None:
            return SmsParseError(reason=error_reason, raw_text=raw_text)

    if not counts:
        return SmsParseError(reason="unrecognized_format", raw_text=raw_text)

    return ParsedSmsUpdate(counts=counts, raw_text=raw_text, locale=locale)
