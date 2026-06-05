"""
research.govcon.universe
========================
A curated universe of PUBLICLY-TRADED U.S. government contractors for the contract-
award event study. Each entry maps a ticker to the recipient-name string(s) that
USAspending files its awards under, plus a cap tier — because the whole hypothesis
is that a given-size award matters to a SMALL-cap and is noise to a prime.

HONESTY ON ENTITY RESOLUTION (the #1 trap): a USAspending "recipient name" is the
legal entity on the contract, which is often a SUBSIDIARY or pre-acquisition name,
not the public parent's ticker. We handle this only partially:
  - `name_search` is matched against the recipient name; `confidence` flags how
    clean that match is (`high` = the public entity files directly under this name;
    `med` = known subsidiary/rename we map by hand; `low` = fuzzy, treat skeptically).
  - We deliberately EXCLUDE names too generic to disambiguate (e.g. "Vertex").
This list is a STARTING universe, not survivorship-free or exhaustive — see the
caveats the event study prints.

cap tiers (rough, by market cap at ~2023):
  small  < ~$2B    mid  ~$2-15B    large  > ~$15B (the primes; expected ~no edge)
"""
from __future__ import annotations

# ticker, name_search (USAspending recipient_search_text), cap tier, confidence, note
UNIVERSE = [
    # --- small-cap: where an award is most likely to be material ---
    ("VSEC", "VSE CORPORATION",                 "small", "high", "verified clean match"),
    ("CVU",  "CPI AEROSTRUCTURES",              "small", "high", ""),
    ("DCO",  "DUCOMMUN",                        "small", "high", ""),
    ("ASTC", "ASTROTECH",                       "small", "high", ""),
    ("KTOS", "KRATOS DEFENSE",                  "small", "high", "drones/space, often material awards"),
    ("AVAV", "AEROVIRONMENT",                   "small", "high", "small UAS"),
    ("MRCY", "MERCURY SYSTEMS",                 "small", "med",  "big awards file under PHYSICAL OPTICS (acquired 2017)"),
    ("UEC",  "URANIUM ENERGY",                  "small", "low",  "fuzzy; energy, weak govcon link"),
    ("NPK",  "NATIONAL PRESTO",                 "small", "low",  "defense is a minority segment"),
    ("TATT", "TAT TECHNOLOGIES",                "small", "low",  ""),
    ("OSIS", "OSI SYSTEMS",                     "mid",   "high", "security/screening"),
    ("VVX",  "VECTRUS",                         "mid",   "med",  "now V2X (VVX); merged with Vertex 2022"),
    ("ICFI", "ICF INTERNATIONAL",               "mid",   "high", ""),
    ("MANT", "MANTECH",                         "mid",   "high", "public until 2022 buyout"),
    ("PSN",  "PARSONS CORPORATION",             "mid",   "high", "public from 2019"),
    ("BWXT", "BWX TECHNOLOGIES",                "mid",   "high", "naval nuclear"),
    ("DRS",  "LEONARDO DRS",                    "mid",   "med",  "public from 2022; files as DRS"),
    ("AIR",  "AAR CORP",                        "mid",   "high", "aviation services"),
    ("TGI",  "TRIUMPH GROUP",                   "mid",   "high", ""),
    ("HII",  "HUNTINGTON INGALLS",              "mid",   "high", "shipbuilder"),

    # --- mid/large IT-services ---
    ("CACI", "CACI",                            "mid",   "high", ""),
    ("SAIC", "SCIENCE APPLICATIONS INTERNATIONAL", "mid", "high", "SAIC"),
    ("LDOS", "LEIDOS",                          "large", "high", ""),
    ("BAH",  "BOOZ ALLEN HAMILTON",             "large", "high", ""),

    # --- large primes: control group, expect ~zero abnormal return (priced in) ---
    ("LMT",  "LOCKHEED MARTIN",                 "large", "high", "prime"),
    ("RTX",  "RAYTHEON",                        "large", "med",  "prime; also files RTX/Collins/Pratt"),
    ("NOC",  "NORTHROP GRUMMAN",                "large", "high", "prime"),
    ("GD",   "GENERAL DYNAMICS",                "large", "high", "prime"),
    ("LHX",  "L3HARRIS",                        "large", "high", "prime"),
    ("HON",  "HONEYWELL",                       "large", "med",  "diversified"),
    ("BA",   "BOEING",                          "large", "med",  "prime; commercial dominates the stock"),
    ("CW",   "CURTISS-WRIGHT",                  "mid",   "high", ""),
    ("HEI",  "HEICO",                           "mid",   "high", ""),
    ("TDG",  "TRANSDIGM",                       "large", "med",  "aftermarket"),
    ("WWD",  "WOODWARD",                        "mid",   "high", ""),
]

# Default event-study thresholds (overridable).
MIN_AWARD_USD = 20_000_000      # an award must be at least this large to count as an "event"
