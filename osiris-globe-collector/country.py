"""Offline country tagging for the OSIRIS globe collector.

Three pure, network-free lookups, each backed by a table precomputed at import
so the per-record cost is O(1) / O(log n) — these run every cycle on the flights
and military_air layers and must never make a network call.

  1. country_from_icao_hex(hex)   -> (country_code, country_name) | (None, None)
       ICAO 24-bit address -> country, via the official ICAO address-block
       allocation ranges (ITU/ICAO Annex 10). Ranges are sorted; lookup is a
       bisect (O(log n)). This is the authoritative aircraft-nationality source.

  2. country_from_registration(tail) -> (country_code, country_name) | (None, None)
       Fallback when the hex is missing/unallocated: the civil registration
       (tail number) prefix -> country, per the ICAO nationality-mark table.
       Longest-prefix match against a precomputed dict.

  3. country_from_latlng(lat, lng) -> (country_code, country_name) | (None, None)
       Offline reverse-geocode for the (static, ~5.8k) military_bases layer.
       Bundled low-res country bounding boxes + centroids; a point is assigned to
       the nearest centroid among the countries whose bbox contains it (falling
       back to nearest centroid overall). No polygons, no network.

The canonical output everywhere is (country_code = ISO 3166-1 alpha-2 UPPERCASE,
country_name) so callers can emit the globe's canonical `country_code` + `flag`
fields directly.
"""
from bisect import bisect_right

# ---------------------------------------------------------------------------
# 1. ICAO 24-bit address -> country.
# Official ICAO Annex 10 Vol III address-block allocations. Each entry is
# (start_hex, end_hex_inclusive, ISO2, country_name). Most countries hold one
# contiguous /XX block; a few (US, China, Russia, ...) hold large national
# ranges. Compiled to a sorted (start_int, end_int, code, name) table at import;
# lookup is a bisect on the start values then an end-bound check.
# ---------------------------------------------------------------------------
_ICAO_BLOCKS_RAW = [
    # (start, end, ISO2, name)
    ("004000", "0043FF", "ZW", "Zimbabwe"),
    ("006000", "006FFF", "MZ", "Mozambique"),
    ("008000", "00FFFF", "ZA", "South Africa"),
    ("010000", "017FFF", "EG", "Egypt"),
    ("018000", "01FFFF", "LY", "Libya"),
    ("020000", "027FFF", "MA", "Morocco"),
    ("028000", "02FFFF", "TN", "Tunisia"),
    ("030000", "0303FF", "BW", "Botswana"),
    ("032000", "032FFF", "BI", "Burundi"),
    ("034000", "034FFF", "CM", "Cameroon"),
    ("035000", "0353FF", "KM", "Comoros"),
    ("036000", "036FFF", "CG", "Congo"),
    ("038000", "038FFF", "CI", "Cote d'Ivoire"),
    ("03E000", "03EFFF", "GA", "Gabon"),
    ("040000", "040FFF", "ET", "Ethiopia"),
    ("042000", "042FFF", "GQ", "Equatorial Guinea"),
    ("044000", "044FFF", "GH", "Ghana"),
    ("046000", "046FFF", "GN", "Guinea"),
    ("048000", "0483FF", "GW", "Guinea-Bissau"),
    ("04A000", "04A3FF", "LS", "Lesotho"),
    ("04C000", "04CFFF", "KE", "Kenya"),
    ("050000", "050FFF", "LR", "Liberia"),
    ("054000", "054FFF", "MG", "Madagascar"),
    ("058000", "058FFF", "MW", "Malawi"),
    ("05A000", "05A3FF", "MV", "Maldives"),
    ("05C000", "05CFFF", "ML", "Mali"),
    ("05E000", "05E3FF", "MR", "Mauritania"),
    ("060000", "0603FF", "MU", "Mauritius"),
    ("062000", "062FFF", "NE", "Niger"),
    ("064000", "064FFF", "NG", "Nigeria"),
    ("068000", "068FFF", "UG", "Uganda"),
    ("06A000", "06A3FF", "QA", "Qatar"),
    ("06C000", "06CFFF", "CF", "Central African Republic"),
    ("06E000", "06EFFF", "RW", "Rwanda"),
    ("070000", "070FFF", "SN", "Senegal"),
    ("074000", "0743FF", "SC", "Seychelles"),
    ("076000", "0763FF", "SL", "Sierra Leone"),
    ("078000", "078FFF", "SO", "Somalia"),
    ("07A000", "07A3FF", "SZ", "Eswatini"),
    ("07C000", "07CFFF", "SD", "Sudan"),
    ("080000", "080FFF", "TZ", "Tanzania"),
    ("084000", "084FFF", "TD", "Chad"),
    ("088000", "088FFF", "TG", "Togo"),
    ("08A000", "08AFFF", "ZM", "Zambia"),
    ("08C000", "08CFFF", "CD", "DR Congo"),
    ("090000", "090FFF", "AO", "Angola"),
    ("094000", "0943FF", "BJ", "Benin"),
    ("096000", "0963FF", "CV", "Cape Verde"),
    ("098000", "0983FF", "DJ", "Djibouti"),
    ("09A000", "09AFFF", "GM", "Gambia"),
    ("09C000", "09CFFF", "BF", "Burkina Faso"),
    ("09E000", "09E3FF", "ST", "Sao Tome & Principe"),
    ("0A0000", "0A7FFF", "DZ", "Algeria"),
    ("0A8000", "0A8FFF", "BS", "Bahamas"),
    ("0AA000", "0AA3FF", "BB", "Barbados"),
    ("0AB000", "0AB3FF", "BZ", "Belize"),
    ("0AC000", "0ACFFF", "CO", "Colombia"),
    ("0AE000", "0AEFFF", "CR", "Costa Rica"),
    ("0B0000", "0B0FFF", "CU", "Cuba"),
    ("0B2000", "0B2FFF", "SV", "El Salvador"),
    ("0B4000", "0B4FFF", "GT", "Guatemala"),
    ("0B6000", "0B6FFF", "GY", "Guyana"),
    ("0B8000", "0B8FFF", "HT", "Haiti"),
    ("0BA000", "0BAFFF", "HN", "Honduras"),
    ("0BC000", "0BC3FF", "VC", "St Vincent & Grenadines"),
    ("0BE000", "0BEFFF", "JM", "Jamaica"),
    ("0C0000", "0C0FFF", "NI", "Nicaragua"),
    ("0C2000", "0C2FFF", "PA", "Panama"),
    ("0C4000", "0C4FFF", "DO", "Dominican Republic"),
    ("0C6000", "0C6FFF", "TT", "Trinidad & Tobago"),
    ("0C8000", "0C8FFF", "SR", "Suriname"),
    ("0CA000", "0CA3FF", "AG", "Antigua & Barbuda"),
    ("0CC000", "0CC3FF", "GD", "Grenada"),
    ("0D0000", "0D0FFF", "MX", "Mexico"),
    ("0D8000", "0DFFFF", "VE", "Venezuela"),
    ("100000", "1FFFFF", "RU", "Russia"),
    ("201000", "2013FF", "NA", "Namibia"),
    ("202000", "2023FF", "ER", "Eritrea"),
    ("300000", "33FFFF", "IT", "Italy"),
    ("340000", "37FFFF", "ES", "Spain"),
    ("380000", "3BFFFF", "FR", "France"),
    ("3C0000", "3FFFFF", "DE", "Germany"),
    ("400000", "43FFFF", "GB", "United Kingdom"),
    ("440000", "447FFF", "AT", "Austria"),
    ("448000", "44FFFF", "BE", "Belgium"),
    ("450000", "457FFF", "BG", "Bulgaria"),
    ("458000", "45FFFF", "DK", "Denmark"),
    ("460000", "467FFF", "FI", "Finland"),
    ("468000", "46FFFF", "GR", "Greece"),
    ("470000", "477FFF", "HU", "Hungary"),
    ("478000", "47FFFF", "NO", "Norway"),
    ("480000", "487FFF", "NL", "Netherlands"),
    ("488000", "48FFFF", "PL", "Poland"),
    ("490000", "497FFF", "PT", "Portugal"),
    ("498000", "49FFFF", "CZ", "Czech Republic"),
    ("4A0000", "4A7FFF", "RO", "Romania"),
    ("4A8000", "4AFFFF", "SE", "Sweden"),
    ("4B0000", "4B7FFF", "CH", "Switzerland"),
    ("4B8000", "4BFFFF", "TR", "Turkey"),
    ("4C0000", "4C7FFF", "RS", "Serbia"),
    ("4C8000", "4C83FF", "CY", "Cyprus"),
    ("4CA000", "4CAFFF", "IE", "Ireland"),
    ("4CC000", "4CCFFF", "IS", "Iceland"),
    ("4D0000", "4D03FF", "LU", "Luxembourg"),
    ("4D2000", "4D23FF", "MT", "Malta"),
    ("4D4000", "4D43FF", "MC", "Monaco"),
    ("500000", "5003FF", "SM", "San Marino"),
    ("501000", "5013FF", "AL", "Albania"),
    ("501C00", "501FFF", "HR", "Croatia"),
    ("502C00", "502FFF", "LV", "Latvia"),
    ("503C00", "503FFF", "LT", "Lithuania"),
    ("504C00", "504FFF", "MD", "Moldova"),
    ("505C00", "505FFF", "SK", "Slovakia"),
    ("506C00", "506FFF", "SI", "Slovenia"),
    ("507C00", "507FFF", "UZ", "Uzbekistan"),
    ("508000", "50FFFF", "UA", "Ukraine"),
    ("510000", "5103FF", "BY", "Belarus"),
    ("511000", "5113FF", "EE", "Estonia"),
    ("512000", "5123FF", "MK", "North Macedonia"),
    ("513000", "5133FF", "BA", "Bosnia & Herzegovina"),
    ("514000", "5143FF", "GE", "Georgia"),
    ("515000", "5153FF", "TJ", "Tajikistan"),
    ("516000", "5163FF", "ME", "Montenegro"),
    ("600000", "6003FF", "AM", "Armenia"),
    ("600800", "600BFF", "AZ", "Azerbaijan"),
    ("601000", "6013FF", "KG", "Kyrgyzstan"),
    ("601800", "601BFF", "TM", "Turkmenistan"),
    ("680000", "6803FF", "BT", "Bhutan"),
    ("681000", "6813FF", "FM", "Micronesia"),
    ("682000", "6823FF", "MN", "Mongolia"),
    ("683000", "6833FF", "KZ", "Kazakhstan"),
    ("684000", "6843FF", "PW", "Palau"),
    ("700000", "700FFF", "AF", "Afghanistan"),
    ("702000", "702FFF", "BD", "Bangladesh"),
    ("704000", "704FFF", "MM", "Myanmar"),
    ("706000", "706FFF", "KW", "Kuwait"),
    ("708000", "708FFF", "LA", "Laos"),
    ("70A000", "70AFFF", "NP", "Nepal"),
    ("70C000", "70C3FF", "OM", "Oman"),
    ("70E000", "70EFFF", "KH", "Cambodia"),
    ("710000", "717FFF", "SA", "Saudi Arabia"),
    ("718000", "71FFFF", "KR", "South Korea"),
    ("720000", "727FFF", "KP", "North Korea"),
    ("728000", "72FFFF", "IQ", "Iraq"),
    ("730000", "737FFF", "IR", "Iran"),
    ("738000", "73FFFF", "IL", "Israel"),
    ("740000", "747FFF", "JO", "Jordan"),
    ("748000", "74FFFF", "LB", "Lebanon"),
    ("750000", "757FFF", "MY", "Malaysia"),
    ("758000", "75FFFF", "PH", "Philippines"),
    ("760000", "767FFF", "PK", "Pakistan"),
    ("768000", "76FFFF", "SG", "Singapore"),
    ("770000", "777FFF", "LK", "Sri Lanka"),
    ("778000", "77FFFF", "SY", "Syria"),
    ("780000", "7BFFFF", "CN", "China"),
    ("7C0000", "7FFFFF", "AU", "Australia"),
    ("800000", "83FFFF", "IN", "India"),
    ("840000", "87FFFF", "JP", "Japan"),
    ("880000", "887FFF", "TH", "Thailand"),
    ("888000", "88FFFF", "VN", "Vietnam"),
    ("890000", "890FFF", "TW", "Taiwan"),
    ("894000", "894FFF", "ID", "Indonesia"),  # part of ID block
    ("896000", "896FFF", "AE", "United Arab Emirates"),
    ("897000", "8973FF", "MO", "Macao"),
    ("898000", "898FFF", "ID", "Indonesia"),
    ("8A0000", "8A7FFF", "ID", "Indonesia"),
    ("900000", "9003FF", "MH", "Marshall Islands"),
    ("901000", "9013FF", "CK", "Cook Islands"),
    ("902000", "9023FF", "WS", "Samoa"),
    ("A00000", "AFFFFF", "US", "United States"),
    ("C00000", "C3FFFF", "CA", "Canada"),
    ("C80000", "C87FFF", "NZ", "New Zealand"),
    ("C88000", "C88FFF", "FJ", "Fiji"),
    ("C8A000", "C8A3FF", "NR", "Nauru"),
    ("C8C000", "C8C3FF", "LC", "St Lucia"),
    ("C8D000", "C8D3FF", "TO", "Tonga"),
    ("C8E000", "C8E3FF", "KI", "Kiribati"),
    ("C90000", "C903FF", "VU", "Vanuatu"),
    ("E00000", "E3FFFF", "AR", "Argentina"),
    ("E40000", "E7FFFF", "BR", "Brazil"),
    ("E80000", "E80FFF", "CL", "Chile"),
    ("E84000", "E84FFF", "EC", "Ecuador"),
    ("E88000", "E88FFF", "PY", "Paraguay"),
    ("E8C000", "E8CFFF", "PE", "Peru"),
    ("E90000", "E90FFF", "UY", "Uruguay"),
    ("E94000", "E94FFF", "BO", "Bolivia"),
    ("F00000", "F07FFF", "MX", "Mexico (ICAO temp)"),  # rarely seen
]


def _build_icao_table():
    rows = []
    for start, end, code, name in _ICAO_BLOCKS_RAW:
        rows.append((int(start, 16), int(end, 16), code, name))
    rows.sort(key=lambda r: r[0])
    starts = [r[0] for r in rows]
    return rows, starts


_ICAO_ROWS, _ICAO_STARTS = _build_icao_table()


def country_from_icao_hex(hexaddr):
    """ICAO 24-bit address (hex string) -> (ISO2, country_name) | (None, None)."""
    if not hexaddr:
        return (None, None)
    s = str(hexaddr).strip().lower()
    # strip a leading '~' (adsb anonymized/TIS-B) and any '0x'
    s = s.lstrip("~")
    if s.startswith("0x"):
        s = s[2:]
    try:
        v = int(s, 16)
    except ValueError:
        return (None, None)
    i = bisect_right(_ICAO_STARTS, v) - 1
    if i < 0:
        return (None, None)
    start, end, code, name = _ICAO_ROWS[i]
    if start <= v <= end:
        return (code, name)
    return (None, None)


# ---------------------------------------------------------------------------
# 2. Registration (tail number) prefix -> country. Fallback when the hex is
# absent/unallocated. ICAO nationality marks; longest-prefix wins. Keys are the
# leading characters of the registration up to the first dash or digit boundary.
# ---------------------------------------------------------------------------
_REG_PREFIXES = {
    # Single-letter / common
    "N": ("US", "United States"),
    "C": ("CA", "Canada"),         # C-, CF-, CG- (Canada); CU=Cuba below (longer)
    "G": ("GB", "United Kingdom"),
    "D": ("DE", "Germany"),        # D-xxxx (note: D2=Angola, longer prefix wins)
    "F": ("FR", "France"),
    "I": ("IT", "Italy"),
    "B": ("CN", "China"),          # B-xxxx (also TW/HK by number; CN by default)
    "M": ("IM", "Isle of Man"),    # M-xxxx
    "P": ("KP", "North Korea"),    # P-xxx (rare)
    # Two-char
    "VH": ("AU", "Australia"),
    "ZK": ("NZ", "New Zealand"), "ZL": ("NZ", "New Zealand"), "ZM": ("NZ", "New Zealand"),
    "JA": ("JP", "Japan"),
    "HL": ("KR", "South Korea"),
    "VT": ("IN", "India"),
    "PK": ("ID", "Indonesia"),
    "RP": ("PH", "Philippines"),
    "HS": ("TH", "Thailand"),
    "9M": ("MY", "Malaysia"),
    "9V": ("SG", "Singapore"),
    "VN": ("VN", "Vietnam"),
    "XU": ("KH", "Cambodia"),
    "AP": ("PK", "Pakistan"),
    "S2": ("BD", "Bangladesh"),
    "4R": ("LK", "Sri Lanka"),
    "EP": ("IR", "Iran"),
    "YI": ("IQ", "Iraq"),
    "JY": ("JO", "Jordan"),
    "OD": ("LB", "Lebanon"),
    "YK": ("SY", "Syria"),
    "4X": ("IL", "Israel"),
    "A4O": ("OM", "Oman"),
    "A6": ("AE", "United Arab Emirates"),
    "A7": ("QA", "Qatar"),
    "A9C": ("BH", "Bahrain"),
    "HZ": ("SA", "Saudi Arabia"),
    "9K": ("KW", "Kuwait"),
    "TC": ("TR", "Turkey"),
    "SU": ("EG", "Egypt"),
    "5A": ("LY", "Libya"),
    "TS": ("TN", "Tunisia"),
    "7T": ("DZ", "Algeria"),
    "CN": ("MA", "Morocco"),
    "ST": ("SD", "Sudan"),
    "ET": ("ET", "Ethiopia"),
    "5Y": ("KE", "Kenya"),
    "5N": ("NG", "Nigeria"),
    "ZS": ("ZA", "South Africa"), "ZT": ("ZA", "South Africa"), "ZU": ("ZA", "South Africa"),
    "9G": ("GH", "Ghana"),
    "TU": ("CI", "Cote d'Ivoire"),
    "6V": ("SN", "Senegal"),
    "D2": ("AO", "Angola"),
    "5H": ("TZ", "Tanzania"),
    "5X": ("UG", "Uganda"),
    "9J": ("ZM", "Zambia"),
    "Z": ("ZW", "Zimbabwe"),
    "5R": ("MG", "Madagascar"),
    "EC": ("ES", "Spain"), "EC-": ("ES", "Spain"),
    "CS": ("PT", "Portugal"),
    "OO": ("BE", "Belgium"),
    "PH": ("NL", "Netherlands"),
    "OE": ("AT", "Austria"),
    "HB": ("CH", "Switzerland"),
    "LX": ("LU", "Luxembourg"),
    "EI": ("IE", "Ireland"), "EJ": ("IE", "Ireland"),
    "OY": ("DK", "Denmark"),
    "LN": ("NO", "Norway"),
    "SE": ("SE", "Sweden"),
    "OH": ("FI", "Finland"),
    "TF": ("IS", "Iceland"),
    "SP": ("PL", "Poland"),
    "OK": ("CZ", "Czech Republic"),
    "OM": ("SK", "Slovakia"),
    "HA": ("HU", "Hungary"),
    "YR": ("RO", "Romania"),
    "LZ": ("BG", "Bulgaria"),
    "SX": ("GR", "Greece"),
    "9A": ("HR", "Croatia"),
    "S5": ("SI", "Slovenia"),
    "YU": ("RS", "Serbia"),
    "Z3": ("MK", "North Macedonia"),
    "ZA": ("AL", "Albania"),
    "E7": ("BA", "Bosnia & Herzegovina"),
    "4O": ("ME", "Montenegro"),
    "5B": ("CY", "Cyprus"),
    "9H": ("MT", "Malta"),
    "RA": ("RU", "Russia"), "RF": ("RU", "Russia"),
    "UR": ("UA", "Ukraine"),
    "EW": ("BY", "Belarus"),
    "ES": ("EE", "Estonia"),
    "YL": ("LV", "Latvia"),
    "LY": ("LT", "Lithuania"),
    "ER": ("MD", "Moldova"),
    "4L": ("GE", "Georgia"),
    "EK": ("AM", "Armenia"),
    "4K": ("AZ", "Azerbaijan"),
    "UK": ("UZ", "Uzbekistan"),
    "UN": ("KZ", "Kazakhstan"),
    "EX": ("KG", "Kyrgyzstan"),
    "EY": ("TJ", "Tajikistan"),
    "EZ": ("TM", "Turkmenistan"),
    "JU": ("MN", "Mongolia"),
    "HK": ("CO", "Colombia"),
    "LV": ("AR", "Argentina"),
    "PP": ("BR", "Brazil"), "PR": ("BR", "Brazil"), "PT": ("BR", "Brazil"), "PS": ("BR", "Brazil"),
    "CC": ("CL", "Chile"),
    "HC": ("EC", "Ecuador"),
    "OB": ("PE", "Peru"),
    "CP": ("BO", "Bolivia"),
    "ZP": ("PY", "Paraguay"),
    "CX": ("UY", "Uruguay"),
    "YV": ("VE", "Venezuela"),
    "XA": ("MX", "Mexico"), "XB": ("MX", "Mexico"), "XC": ("MX", "Mexico"),
    "CU": ("CU", "Cuba"),
    "TI": ("CR", "Costa Rica"),
    "HP": ("PA", "Panama"),
    "HI": ("DO", "Dominican Republic"),
    "9Y": ("TT", "Trinidad & Tobago"),
}
# Longest keys first so e.g. "A6" beats "A", "D2" beats "D".
_REG_KEYS = sorted(_REG_PREFIXES.keys(), key=len, reverse=True)


def country_from_registration(tail):
    """Civil registration / tail number -> (ISO2, name) | (None, None)."""
    if not tail:
        return (None, None)
    t = str(tail).strip().upper()
    if not t:
        return (None, None)
    for k in _REG_KEYS:
        if t.startswith(k):
            return _REG_PREFIXES[k]
    return (None, None)


def country_from_aircraft(hexaddr=None, tail=None):
    """Best-effort aircraft nationality: ICAO hex first, registration fallback.
    Returns (ISO2, name) | (None, None)."""
    code, name = country_from_icao_hex(hexaddr)
    if code:
        return (code, name)
    return country_from_registration(tail)


# ---------------------------------------------------------------------------
# 3. Offline lat/lng -> country for the static military_bases layer.
# Bundled low-res table: (ISO2, name, lat_min, lat_max, lng_min, lng_max,
# centroid_lat, centroid_lng). A point is assigned to the nearest centroid among
# the countries whose bounding box contains it; if none contains it (coastal
# rounding, small islands), the globally nearest centroid is used. This is a
# coarse but fully offline reverse geocode adequate for tagging installations to
# a nation. Bounding boxes are intentionally generous; the centroid tie-break
# resolves overlaps (e.g. nested/adjacent countries).
# ---------------------------------------------------------------------------
# (ISO2, name, lat_min, lat_max, lng_min, lng_max, clat, clng)
_COUNTRY_BBOX = [
    ("US", "United States", 24.5, 49.4, -125.0, -66.9, 39.8, -98.6),
    ("US", "United States", 51.0, 71.4, -179.9, -129.9, 64.2, -152.0),  # Alaska
    ("US", "United States", 18.9, 22.3, -160.3, -154.8, 20.7, -157.5),  # Hawaii
    ("CA", "Canada", 41.7, 70.0, -141.0, -52.6, 56.1, -106.3),
    ("MX", "Mexico", 14.5, 32.7, -118.4, -86.7, 23.6, -102.5),
    ("GT", "Guatemala", 13.7, 17.8, -92.2, -88.2, 15.5, -90.3),
    ("CU", "Cuba", 19.8, 23.3, -85.0, -74.1, 21.5, -79.0),
    ("HT", "Haiti", 18.0, 20.1, -74.5, -71.6, 19.1, -72.3),
    ("DO", "Dominican Republic", 17.5, 19.9, -72.0, -68.3, 18.7, -70.2),
    ("PA", "Panama", 7.2, 9.7, -83.1, -77.1, 8.5, -80.0),
    ("CR", "Costa Rica", 8.0, 11.2, -85.9, -82.5, 9.7, -83.8),
    ("CO", "Colombia", -4.3, 12.6, -79.0, -66.9, 4.6, -74.3),
    ("VE", "Venezuela", 0.6, 12.2, -73.4, -59.8, 6.4, -66.6),
    ("BR", "Brazil", -33.8, 5.3, -74.0, -34.8, -10.8, -52.9),
    ("AR", "Argentina", -55.1, -21.8, -73.6, -53.6, -38.4, -63.6),
    ("CL", "Chile", -55.9, -17.5, -75.7, -66.4, -35.7, -71.5),
    ("PE", "Peru", -18.4, -0.0, -81.4, -68.7, -9.2, -75.0),
    ("BO", "Bolivia", -22.9, -9.7, -69.6, -57.5, -16.3, -64.0),
    ("EC", "Ecuador", -5.0, 1.7, -81.1, -75.2, -1.8, -78.2),
    ("PY", "Paraguay", -27.6, -19.3, -62.6, -54.3, -23.4, -58.4),
    ("UY", "Uruguay", -35.0, -30.1, -58.5, -53.1, -32.5, -55.8),
    ("GB", "United Kingdom", 49.9, 60.9, -8.6, 1.8, 54.0, -2.0),
    ("IE", "Ireland", 51.4, 55.4, -10.5, -6.0, 53.2, -8.0),
    ("FR", "France", 41.3, 51.1, -5.1, 9.6, 46.6, 2.2),
    ("ES", "Spain", 36.0, 43.8, -9.3, 3.3, 40.2, -3.7),
    ("PT", "Portugal", 36.9, 42.2, -9.6, -6.2, 39.6, -8.0),
    ("DE", "Germany", 47.3, 55.1, 5.9, 15.0, 51.2, 10.4),
    ("IT", "Italy", 36.6, 47.1, 6.6, 18.5, 42.8, 12.6),
    ("CH", "Switzerland", 45.8, 47.8, 5.9, 10.5, 46.8, 8.2),
    ("AT", "Austria", 46.4, 49.0, 9.5, 17.2, 47.6, 14.1),
    ("BE", "Belgium", 49.5, 51.5, 2.5, 6.4, 50.5, 4.5),
    ("NL", "Netherlands", 50.8, 53.6, 3.4, 7.2, 52.1, 5.3),
    ("DK", "Denmark", 54.6, 57.8, 8.1, 12.7, 56.0, 9.5),
    ("NO", "Norway", 57.9, 71.2, 4.6, 31.1, 60.5, 8.5),
    ("SE", "Sweden", 55.3, 69.1, 11.1, 24.2, 62.0, 15.0),
    ("FI", "Finland", 59.8, 70.1, 20.6, 31.6, 64.0, 26.0),
    ("PL", "Poland", 49.0, 54.9, 14.1, 24.2, 52.1, 19.4),
    ("CZ", "Czech Republic", 48.5, 51.1, 12.1, 18.9, 49.8, 15.5),
    ("SK", "Slovakia", 47.7, 49.6, 16.8, 22.6, 48.7, 19.7),
    ("HU", "Hungary", 45.7, 48.6, 16.1, 22.9, 47.2, 19.5),
    ("RO", "Romania", 43.6, 48.3, 20.3, 29.7, 45.9, 25.0),
    ("BG", "Bulgaria", 41.2, 44.2, 22.4, 28.6, 42.7, 25.5),
    ("GR", "Greece", 34.8, 41.8, 19.4, 28.3, 39.1, 22.0),
    ("HR", "Croatia", 42.4, 46.6, 13.5, 19.4, 45.1, 15.2),
    ("RS", "Serbia", 42.2, 46.2, 18.8, 23.0, 44.0, 21.0),
    ("SI", "Slovenia", 45.4, 46.9, 13.4, 16.6, 46.1, 14.8),
    ("UA", "Ukraine", 44.4, 52.4, 22.1, 40.2, 49.0, 32.0),
    ("BY", "Belarus", 51.3, 56.2, 23.2, 32.8, 53.7, 28.0),
    ("LT", "Lithuania", 53.9, 56.5, 21.0, 26.8, 55.2, 23.9),
    ("LV", "Latvia", 55.7, 58.1, 21.0, 28.2, 56.9, 24.6),
    ("EE", "Estonia", 57.5, 59.7, 21.8, 28.2, 58.6, 25.0),
    ("MD", "Moldova", 45.5, 48.5, 26.6, 30.1, 47.4, 28.4),
    ("RU", "Russia", 41.2, 77.7, 27.0, 180.0, 61.5, 90.0),
    ("RU", "Russia", 41.2, 77.7, -180.0, -169.0, 66.0, -173.0),  # Chukotka wrap
    ("KZ", "Kazakhstan", 40.6, 55.4, 46.5, 87.3, 48.0, 67.0),
    ("TR", "Turkey", 35.8, 42.1, 25.7, 44.8, 39.0, 35.2),
    ("GE", "Georgia", 41.0, 43.6, 40.0, 46.7, 42.3, 43.4),
    ("AM", "Armenia", 38.8, 41.3, 43.4, 46.6, 40.1, 45.0),
    ("AZ", "Azerbaijan", 38.4, 41.9, 44.8, 50.4, 40.1, 47.6),
    ("IS", "Iceland", 63.3, 66.6, -24.5, -13.5, 64.9, -19.0),
    ("EG", "Egypt", 22.0, 31.7, 25.0, 36.9, 26.8, 30.8),
    ("LY", "Libya", 19.5, 33.2, 9.3, 25.2, 27.0, 17.3),
    ("TN", "Tunisia", 30.2, 37.5, 7.5, 11.6, 33.9, 9.6),
    ("DZ", "Algeria", 18.9, 37.1, -8.7, 12.0, 28.0, 1.7),
    ("MA", "Morocco", 27.6, 35.9, -13.2, -1.0, 31.8, -7.1),
    ("SD", "Sudan", 8.7, 22.2, 21.8, 38.6, 15.5, 30.2),
    ("SS", "South Sudan", 3.5, 12.2, 24.1, 35.9, 7.3, 30.3),
    ("ET", "Ethiopia", 3.4, 14.9, 33.0, 48.0, 9.1, 40.5),
    ("ER", "Eritrea", 12.4, 18.0, 36.4, 43.1, 15.2, 39.8),
    ("DJ", "Djibouti", 10.9, 12.7, 41.7, 43.4, 11.8, 42.6),
    ("SO", "Somalia", -1.7, 12.0, 41.0, 51.4, 5.2, 46.2),
    ("KE", "Kenya", -4.7, 5.0, 33.9, 41.9, 0.0, 37.9),
    ("TZ", "Tanzania", -11.7, -1.0, 29.3, 40.4, -6.4, 34.9),
    ("UG", "Uganda", -1.5, 4.2, 29.6, 35.0, 1.4, 32.3),
    ("NG", "Nigeria", 4.3, 13.9, 2.7, 14.7, 9.1, 8.7),
    ("GH", "Ghana", 4.7, 11.2, -3.3, 1.2, 7.9, -1.0),
    ("CI", "Cote d'Ivoire", 4.4, 10.7, -8.6, -2.5, 7.5, -5.5),
    ("SN", "Senegal", 12.3, 16.7, -17.5, -11.4, 14.5, -14.5),
    ("ML", "Mali", 10.2, 25.0, -12.2, 4.3, 17.6, -4.0),
    ("NE", "Niger", 11.7, 23.5, 0.2, 16.0, 17.6, 8.1),
    ("TD", "Chad", 7.4, 23.4, 13.5, 24.0, 15.5, 18.7),
    ("CM", "Cameroon", 1.7, 13.1, 8.5, 16.2, 5.7, 12.7),
    ("CD", "DR Congo", -13.5, 5.4, 12.2, 31.3, -4.0, 21.8),
    ("CG", "Congo", -5.0, 3.7, 11.1, 18.6, -0.7, 15.8),
    ("AO", "Angola", -18.0, -4.4, 11.7, 24.1, -11.2, 17.9),
    ("ZA", "South Africa", -34.8, -22.1, 16.5, 32.9, -30.6, 22.9),
    ("NA", "Namibia", -28.9, -16.9, 11.7, 25.3, -22.0, 18.5),
    ("BW", "Botswana", -26.0, -17.8, 19.9, 29.4, -22.3, 24.7),
    ("ZW", "Zimbabwe", -22.4, -15.6, 25.2, 33.1, -19.0, 29.9),
    ("ZM", "Zambia", -18.1, -8.2, 21.9, 33.7, -13.1, 27.8),
    ("MZ", "Mozambique", -26.9, -10.5, 30.2, 40.8, -18.7, 35.5),
    ("MG", "Madagascar", -25.6, -11.9, 43.2, 50.5, -18.8, 46.9),
    ("SA", "Saudi Arabia", 16.4, 32.2, 34.6, 55.7, 24.0, 45.0),
    ("YE", "Yemen", 12.1, 19.0, 42.5, 54.5, 15.6, 47.6),
    ("OM", "Oman", 16.6, 26.4, 52.0, 59.8, 21.5, 55.9),
    ("AE", "United Arab Emirates", 22.6, 26.1, 51.5, 56.4, 24.0, 54.0),
    ("QA", "Qatar", 24.5, 26.2, 50.7, 51.6, 25.3, 51.2),
    ("KW", "Kuwait", 28.5, 30.1, 46.5, 48.4, 29.3, 47.5),
    ("BH", "Bahrain", 25.8, 26.3, 50.4, 50.8, 26.0, 50.6),
    ("IQ", "Iraq", 29.1, 37.4, 38.8, 48.6, 33.2, 43.7),
    ("IR", "Iran", 25.1, 39.8, 44.0, 63.3, 32.4, 53.7),
    ("SY", "Syria", 32.3, 37.3, 35.7, 42.4, 35.0, 38.0),
    ("JO", "Jordan", 29.2, 33.4, 34.9, 39.3, 31.3, 36.2),
    ("LB", "Lebanon", 33.0, 34.7, 35.1, 36.6, 33.9, 35.9),
    ("IL", "Israel", 29.5, 33.3, 34.3, 35.9, 31.4, 35.0),
    ("AF", "Afghanistan", 29.4, 38.5, 60.5, 74.9, 33.9, 67.7),
    ("PK", "Pakistan", 23.7, 37.1, 60.9, 77.8, 30.4, 69.3),
    ("IN", "India", 6.7, 35.5, 68.1, 97.4, 22.4, 78.9),
    ("BD", "Bangladesh", 20.6, 26.6, 88.0, 92.7, 23.7, 90.4),
    ("LK", "Sri Lanka", 5.9, 9.9, 79.6, 81.9, 7.9, 80.8),
    ("NP", "Nepal", 26.3, 30.4, 80.0, 88.2, 28.4, 84.1),
    ("CN", "China", 18.2, 53.6, 73.5, 134.8, 35.9, 104.2),
    ("MN", "Mongolia", 41.6, 52.2, 87.7, 119.9, 46.9, 103.8),
    ("KP", "North Korea", 37.7, 43.0, 124.3, 130.7, 40.3, 127.5),
    ("KR", "South Korea", 33.1, 38.6, 125.9, 129.6, 36.5, 127.8),
    ("JP", "Japan", 30.0, 45.5, 129.4, 145.8, 36.2, 138.3),
    ("TW", "Taiwan", 21.9, 25.3, 120.0, 122.0, 23.7, 121.0),
    ("MM", "Myanmar", 9.8, 28.5, 92.2, 101.2, 21.9, 95.9),
    ("TH", "Thailand", 5.6, 20.5, 97.3, 105.6, 15.9, 101.0),
    ("VN", "Vietnam", 8.2, 23.4, 102.1, 109.5, 16.2, 107.8),
    ("KH", "Cambodia", 10.4, 14.7, 102.3, 107.6, 12.6, 104.9),
    ("LA", "Laos", 13.9, 22.5, 100.1, 107.7, 19.9, 102.5),
    ("MY", "Malaysia", 0.8, 7.4, 99.6, 119.3, 4.2, 101.9),
    ("SG", "Singapore", 1.2, 1.5, 103.6, 104.1, 1.35, 103.8),
    ("ID", "Indonesia", -11.0, 6.1, 95.0, 141.0, -2.5, 118.0),
    ("PH", "Philippines", 4.6, 21.1, 116.9, 126.6, 12.9, 121.8),
    ("AU", "Australia", -43.6, -10.7, 113.3, 153.6, -25.3, 133.8),
    ("NZ", "New Zealand", -47.3, -34.4, 166.4, 178.6, -41.0, 174.0),
    ("PG", "Papua New Guinea", -11.6, -1.3, 140.9, 155.9, -6.3, 143.9),
    ("FJ", "Fiji", -19.2, -16.1, 177.0, 180.0, -17.7, 178.0),
]


def country_from_latlng(lat, lng):
    """Offline reverse geocode (ISO2, name) | (None, None) for the bases layer.
    Nearest centroid among bbox-containing countries; global-nearest fallback."""
    try:
        la, lo = float(lat), float(lng)
    except (TypeError, ValueError):
        return (None, None)
    best_in = None
    best_in_d = None
    best_any = None
    best_any_d = None
    for code, name, la0, la1, lo0, lo1, clat, clng in _COUNTRY_BBOX:
        d = (la - clat) ** 2 + (lo - clng) ** 2
        if best_any_d is None or d < best_any_d:
            best_any_d, best_any = d, (code, name)
        if la0 <= la <= la1 and lo0 <= lo <= lo1:
            if best_in_d is None or d < best_in_d:
                best_in_d, best_in = d, (code, name)
    if best_in:
        return best_in
    # Only accept a no-bbox-match nearest-centroid hit if it is reasonably close
    # (within ~12 degrees) so a mid-ocean point isn't force-tagged to a far land.
    if best_any and best_any_d is not None and best_any_d <= 144.0:
        return best_any
    return (None, None)
