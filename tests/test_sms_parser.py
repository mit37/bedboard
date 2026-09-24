"""Tests for app.sms.parser and app.sms.messages (BB-5 / BB-10).

No dependency on app.mock_fabt, app.sms.webhook, or any other in-progress
module -- these tests exercise parser.py and messages.py directly against
app.schemas only.
"""

from __future__ import annotations

from app.schemas import Locale, ParsedSmsUpdate, SmsParseError, SmsPopulationType
from app.sms.messages import (
    render_confirmation,
    render_help,
    render_rejected_unknown_number,
)
from app.sms.parser import parse_sms

W = SmsPopulationType.WOMEN
M = SmsPopulationType.MEN
F = SmsPopulationType.FAMILY


def _assert_parsed(result, expected_counts: dict[SmsPopulationType, int], locale: Locale) -> None:
    assert isinstance(result, ParsedSmsUpdate), f"expected ParsedSmsUpdate, got {result!r}"
    assert result.counts == expected_counts
    assert result.locale == locale


def _assert_error(result, reason: str | None = None) -> None:
    assert isinstance(result, SmsParseError), f"expected SmsParseError, got {result!r}"
    if reason is not None:
        assert result.reason == reason


# --- English acceptance-test formats (literal PRD inputs) --------------


def test_en_spaced_letter_code_all_buckets():
    result = parse_sms("W 3 M 1 F 0", Locale.EN)
    _assert_parsed(result, {W: 3, M: 1, F: 0}, Locale.EN)


def test_en_compact_lowercase_letter_code_all_buckets():
    result = parse_sms("w3m1f0", Locale.EN)
    _assert_parsed(result, {W: 3, M: 1, F: 0}, Locale.EN)


def test_en_compact_partial_two_of_three_buckets():
    result = parse_sms("w3m1", Locale.EN)
    _assert_parsed(result, {W: 3, M: 1}, Locale.EN)
    assert F not in result.counts


def test_en_single_bucket_word_form():
    result = parse_sms("women 3", Locale.EN)
    _assert_parsed(result, {W: 3}, Locale.EN)


def test_en_men_word_form():
    result = parse_sms("men 5", Locale.EN)
    _assert_parsed(result, {M: 5}, Locale.EN)


def test_en_family_word_form():
    result = parse_sms("family 2", Locale.EN)
    _assert_parsed(result, {F: 2}, Locale.EN)


def test_en_mixed_case_and_extra_whitespace():
    result = parse_sms("  WoMeN   3  ", Locale.EN)
    _assert_parsed(result, {W: 3}, Locale.EN)


def test_en_multiple_buckets_word_form_extra_whitespace():
    result = parse_sms("women   3   men  1   family    0", Locale.EN)
    _assert_parsed(result, {W: 3, M: 1, F: 0}, Locale.EN)


def test_en_letter_code_uppercase_no_spaces_mixed_with_values():
    result = parse_sms("W3M1F0", Locale.EN)
    _assert_parsed(result, {W: 3, M: 1, F: 0}, Locale.EN)


# --- Spanish (es) --------------------------------------------------------


def test_es_letter_code_all_buckets():
    result = parse_sms("M 3 H 1 F 0", Locale.ES)
    _assert_parsed(result, {W: 3, M: 1, F: 0}, Locale.ES)


def test_es_compact_letter_code_lowercase():
    result = parse_sms("m3h1f0", Locale.ES)
    _assert_parsed(result, {W: 3, M: 1, F: 0}, Locale.ES)


def test_es_word_form_single_bucket():
    result = parse_sms("mujeres 4", Locale.ES)
    _assert_parsed(result, {W: 4}, Locale.ES)


def test_es_word_form_all_buckets():
    result = parse_sms("mujeres 3 hombres 1 familias 0", Locale.ES)
    _assert_parsed(result, {W: 3, M: 1, F: 0}, Locale.ES)


def test_es_falls_back_to_english_tokens():
    # Coordinator registered as es, but texts in English.
    result = parse_sms("women 6", Locale.ES)
    _assert_parsed(result, {W: 6}, Locale.ES)
    assert result.locale == Locale.ES


# --- Vietnamese (vi), ASCII-safe tokens ----------------------------------


def test_vi_letter_code_all_buckets():
    result = parse_sms("N 3 D 1 G 0", Locale.VI)
    _assert_parsed(result, {W: 3, M: 1, F: 0}, Locale.VI)


def test_vi_compact_letter_code_lowercase():
    result = parse_sms("n3d1g0", Locale.VI)
    _assert_parsed(result, {W: 3, M: 1, F: 0}, Locale.VI)


def test_vi_word_form_single_bucket():
    result = parse_sms("nu 3", Locale.VI)
    _assert_parsed(result, {W: 3}, Locale.VI)


def test_vi_word_form_men_and_family():
    result = parse_sms("nam 5 gia dinh 2", Locale.VI)
    _assert_parsed(result, {M: 5, F: 2}, Locale.VI)


def test_vi_falls_back_to_english_tokens():
    result = parse_sms("men 7", Locale.VI)
    _assert_parsed(result, {M: 7}, Locale.VI)
    assert result.locale == Locale.VI


# --- Garbage / invalid input --------------------------------------------


def test_empty_string_is_error():
    _assert_error(parse_sms("", Locale.EN), reason="empty")


def test_whitespace_only_is_error():
    _assert_error(parse_sms("   ", Locale.EN), reason="empty")


def test_gibberish_is_unrecognized():
    _assert_error(parse_sms("asdkfjasdf", Locale.EN), reason="unrecognized_format")


def test_wrong_locale_keywords_unrecognized():
    # Coordinator registered as en; texting Vietnamese tokens isn't English
    # and English is already the primary table for en, so no further
    # fallback applies -- this must be unrecognized, not silently matched.
    _assert_error(parse_sms("nu 3", Locale.EN), reason="unrecognized_format")


def test_negative_count_is_error():
    _assert_error(parse_sms("W -3", Locale.EN), reason="negative_count")


def test_negative_count_compact_form_is_error():
    _assert_error(parse_sms("men -1", Locale.EN), reason="negative_count")


def test_out_of_range_count_is_error():
    _assert_error(parse_sms("women 501", Locale.EN), reason="out_of_range")


def test_count_at_boundary_500_is_valid():
    result = parse_sms("women 500", Locale.EN)
    _assert_parsed(result, {W: 500}, Locale.EN)


def test_five_digit_count_is_rejected_not_silently_truncated():
    # Regression: `\d{1,4}` alone would greedily match "0000" out of
    # "00005" (a count of 0) and stop there instead of failing to match --
    # silently reporting the wrong count instead of rejecting the message.
    _assert_error(parse_sms("women 00005", Locale.EN), reason="unrecognized_format")
    _assert_error(parse_sms("women 12345", Locale.EN), reason="unrecognized_format")


def test_parse_sms_never_raises_on_weird_input():
    for junk in ["!!!", "12345", "womenwomenwomen", "W M F", "\t\n"]:
        result = parse_sms(junk, Locale.EN)
        assert isinstance(result, (ParsedSmsUpdate, SmsParseError))


# --- Partial-update semantics (explicit) ---------------------------------


def test_partial_update_missing_bucket_is_absent_not_defaulted():
    result = parse_sms("women 3", Locale.EN)
    assert isinstance(result, ParsedSmsUpdate)
    assert result.counts == {W: 3}
    assert M not in result.counts
    assert F not in result.counts
    assert len(result.counts) == 1


def test_partial_update_two_buckets_third_absent():
    result = parse_sms("w3m1", Locale.EN)
    assert isinstance(result, ParsedSmsUpdate)
    assert result.counts == {W: 3, M: 1}
    assert F not in result.counts


# --- messages.py ----------------------------------------------------------


def test_render_confirmation_full_update_en():
    text = render_confirmation({W: 3, M: 1, F: 0}, Locale.EN)
    assert text == "Saved: 3 women, 1 men, 0 family"


def test_render_confirmation_partial_update_en_order_independent_of_dict_order():
    # Insert out of women/men/family order to prove output order is fixed.
    text = render_confirmation({F: 0, W: 3}, Locale.EN)
    assert text == "Saved: 3 women, 0 family"


def test_render_confirmation_es():
    text = render_confirmation({W: 3, M: 1, F: 0}, Locale.ES)
    assert text.startswith("Guardado:")
    assert "mujeres" in text
    assert "hombres" in text
    assert "familias" in text


def test_render_confirmation_vi():
    text = render_confirmation({W: 3, M: 1, F: 0}, Locale.VI)
    assert "nu" in text
    assert "nam" in text
    assert "gia dinh" in text


def test_render_help_all_locales_nonempty_and_distinct():
    en = render_help(Locale.EN)
    es = render_help(Locale.ES)
    vi = render_help(Locale.VI)
    assert en and es and vi
    assert len({en, es, vi}) == 3


def test_render_rejected_unknown_number_defaults_to_en():
    text = render_rejected_unknown_number(Locale.EN)
    assert "BedBoard" in text
    assert text == render_rejected_unknown_number()


def test_render_rejected_unknown_number_es_and_vi_are_translated():
    en = render_rejected_unknown_number(Locale.EN)
    es = render_rejected_unknown_number(Locale.ES)
    vi = render_rejected_unknown_number(Locale.VI)
    assert en != es
    assert en != vi
    assert es != vi
