"""Normalization helpers so '980 West Michigan Avenue' and '980 W Michigan Ave' compare equal."""
from __future__ import annotations

import difflib
import re

STREET_WORDS = {
    "street": "st", "avenue": "ave", "av": "ave", "road": "rd", "drive": "dr", "lane": "ln",
    "boulevard": "blvd", "court": "ct", "place": "pl", "highway": "hwy", "parkway": "pkwy",
    "pk": "pike", "circle": "cir", "terrace": "ter", "suite": "ste", "saint": "st",
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northwest": "nw", "northeast": "ne", "southwest": "sw", "southeast": "se",
}

NAME_WORDS = {"centre": "center", "rehab": "rehabilitation", "&": "and"}

# Website wording -> CRM care_type picklist value.
CARE_MAP = {
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "independent living": "Independent Living",
}


def norm_street(street) -> str:
    words = re.sub(r"[^a-z0-9 ]", " ", str(street or "").lower()).split()
    return " ".join(STREET_WORDS.get(w, w) for w in words)


def norm_zip(zip_code) -> str:
    match = re.match(r"\s*(\d{5})", str(zip_code or ""))
    return match.group(1) if match else ""


def norm_city(city) -> str:
    return re.sub(r"[^a-z ]", "", str(city or "").lower()).strip()


def norm_name(name) -> str:
    text = str(name or "").lower().replace("&", " and ").replace("health care", "healthcare")
    words = re.sub(r"[^a-z0-9 ]", " ", text).split()
    words = [NAME_WORDS.get(w, w) for w in words]
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def norm_phone(phone) -> str:
    return re.sub(r"\D", "", str(phone or ""))


def name_similarity(a, b) -> float:
    return round(difflib.SequenceMatcher(None, norm_name(a), norm_name(b)).ratio(), 2)


def is_po_box(street) -> bool:
    return bool(re.match(r"\s*p\.?\s*o\.?\s*box", str(street or ""), re.I))


def map_care(offering) -> str:
    return CARE_MAP.get(str(offering or "").strip().lower(), "")
