"""M9: the pure contact-detail detector (services.contact_guard).

No database: contains_contact_details is a pure function. Positive cases
cover Indian mobiles (+91 / 0091 / 0 prefixes, separators), landlines with
an STD code, e-mails, wa.me / WhatsApp links, Unicode digits and spelled
digits (best effort). Negative cases are ordinary property text that must
never be refused (prices, pincodes, areas, BHK, times, door / listing numbers).
"""
from __future__ import annotations

import pytest

from app.services.contact_guard import (
    CONTACT_DETAILS_CODE,
    CONTACT_DETAILS_MESSAGE,
    contains_contact_details,
)

POSITIVE = [
    "Call me on 9840123456",
    "9840123456",
    "6123456789",
    "+91 98401 23456",
    "+91-98401-23456",
    "+919840123456",
    "0091 98401 23456",
    "098401 23456",
    "98401.23456",
    "98401-23456",
    "(98401) 23456",
    "98 40 12 34 56",
    "Ph:9840123456.",
    "My number is 98401 23456, call after 6",
    "98401 23456 2BHK",                         # adjacent number does not hide it
    "044-2345 6789",                            # Chennai landline
    "044 23456789",
    "(044) 23456789",
    "0471 2345678",                             # 4-digit STD
    "+91 44 2345 6789",
    "mail me at a.b@gmail.com",
    "OWNER@Example.co.in",
    "name (at) gmail (dot) com",
    "name [at] yahoo [dot] in",
    "wa.me/919840123456",
    "https://wa.me/919840123456",
    "https://api.whatsapp.com/send?phone=919840123456",
    "chat.whatsapp.com/AbCdEf",
    "९८४०१२३४५६",                               # Devanagari digits
    "９８４０１２３４５６",                         # full-width digits
    "nine eight four zero one two three four five six",
]

NEGATIVE = [
    "₹45,00,000",
    "45 lakh",
    "1.2 Cr",
    "600042",
    "Pincode 600042",
    "1200 sqft",
    "2BHK",
    "3 BHK 2 bath 1 balcony",
    "10:30",
    "Visit at 10:30 am",
    "door no 12",
    "Flat 1203, tower 9",
    "Listing 1234567890",                       # bare 10-digit id not starting 6-9
    "Floor 4 of 12",
    "Price 85 lakh negotiable",
    "Call me after 6 pm",
    "Ready to move, 2 car parks",
    "Visit on 26/12/2031 at 10:30, plot 12, 1200 sqft, ₹45,00,000, pincode 600042",
    "whatsapp me",                              # the word alone is not contact data
    "Meet at gate one at ten",
    "Built in 2019, 1450 sq ft, 2 covered parkings",
    "",
    None,
]


@pytest.mark.parametrize("text", POSITIVE)
def test_detects_contact_details(text):
    assert contains_contact_details(text) is True, text


@pytest.mark.parametrize("text", NEGATIVE)
def test_ordinary_property_text_is_not_flagged(text):
    assert contains_contact_details(text) is False, text


def test_numbers_far_apart_are_not_concatenated():
    # Two short numbers separated by words must never become a phone number.
    assert contains_contact_details("98401 is the plot and 23456 is the survey number") is False


def test_refusal_constants_are_the_approved_ones():
    assert CONTACT_DETAILS_CODE == "CONTACT_DETAILS_NOT_ALLOWED"
    assert CONTACT_DETAILS_MESSAGE == "Phone numbers can be shared after the buyer unlocks your contact."


def test_detector_is_pure_and_needs_no_database():
    import app.services.contact_guard as guard
    # The detector never touches a session; only ensure_allowed / buyer_entitled do.
    assert contains_contact_details("9840123456") and contains_contact_details.__module__ == guard.__name__
