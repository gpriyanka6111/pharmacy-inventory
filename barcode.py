"""
Turn whatever the scanner sends into a product.

Handles:
  - 12-digit UPC-A on the bottle (e.g. 361958250115)
  - 11- or 13/14-digit variants some scanners send (missing or extra leading zeros)
  - 2D DataMatrix (DSCSA) strings, e.g. "0100361958250115172712311034ABC" -> GTIN, expiry, lot

How a UPC relates to the NDC:
  UPC  = "3" + 10-digit NDC + check digit       361958250115 -> 6195825011
  The 10-digit NDC can be 4-4-2, 5-3-2 or 5-4-1, and Cardinal stores the 11-digit
  form (padded 5-4-2), so we try all three paddings and see which one exists.
"""
import re

from sqlalchemy import or_, select

from db import Product

GS1_GROUP_SEPARATOR = "\x1d"


def parse_datamatrix(raw):
    """Pull GTIN (01), expiry (17) and lot (10) out of a GS1 DataMatrix string."""
    s = raw.replace("]d2", "").replace(GS1_GROUP_SEPARATOR, "|")
    m = re.match(r"^01(\d{14})", s)
    if not m:
        return None
    info = {"gtin": m.group(1)}
    exp = re.search(r"17(\d{6})", s[16:])
    if exp:
        yy, mm, dd = exp.group(1)[:2], exp.group(1)[2:4], exp.group(1)[4:]
        info["expiry"] = f"20{yy}-{mm}-{'01' if dd == '00' else dd}"
    lot = re.search(r"10([^|]{1,20})", s[16:])
    if lot:
        info["lot"] = lot.group(1)
    return info


def ndc10_to_ndc11_candidates(ndc10):
    """All possible 11-digit NDCs for a 10-digit NDC (segment format is unknown)."""
    return [
        "0" + ndc10,                          # 4-4-2 -> 0xxxx-xxxx-xx
        ndc10[:5] + "0" + ndc10[5:],          # 5-3-2 -> xxxxx-0xxx-xx
        ndc10[:9] + "0" + ndc10[9:],          # 5-4-1 -> xxxxx-xxxx-0x
    ]


def normalize_scan(raw):
    """Return (code to match on UPC column, list of candidate 11-digit NDCs, extra info dict)."""
    raw = raw.strip()
    extra = {}

    dm = parse_datamatrix(raw)
    if dm:
        extra = {k: v for k, v in dm.items() if k != "gtin"}
        digits = dm["gtin"]
    else:
        digits = re.sub(r"\D", "", raw)

    # Normalize to a 12-digit UPC-A where possible
    # EAN-13 / GTIN-14 versions of a UPC just have extra leading zeros
    if len(digits) in (13, 14) and set(digits[:-12]) == {"0"}:
        digits = digits[-12:]
    elif len(digits) == 14 and digits[0] == "0":   # GTIN-14 of a 13-digit EAN
        digits = digits[1:]

    # Any code is first matched directly against the UPC column (covers 12-digit UPC-A,
    # 13-digit EAN on imported consumer items, and odd short codes Cardinal has).
    upc = digits or None
    candidates = []

    if len(digits) == 12 and digits[0] == "3":
        candidates = ndc10_to_ndc11_candidates(upc[1:11])
    elif len(digits) == 11:
        candidates = [digits]                 # typed / scanned 11-digit NDC
    elif len(digits) == 10:
        candidates = ndc10_to_ndc11_candidates(digits)

    return upc, candidates, extra


def find_product(session, raw_scan):
    """Look up a scanned barcode. Returns (Product or None, extra info like lot/expiry)."""
    upc, ndc_candidates, extra = normalize_scan(raw_scan)

    # Match the trimmed code and the raw digits (Cardinal sometimes stores 14-digit GTINs)
    upc_codes = {c for c in (upc, re.sub(r"\D", "", raw_scan)) if c}

    conditions = []
    if upc_codes:
        conditions.append(Product.upc.in_(upc_codes))
    if ndc_candidates:
        conditions.append(Product.ndc.in_(ndc_candidates))
    if not conditions:
        return None, extra

    matches = session.scalars(select(Product).where(or_(*conditions))).all()
    if not matches:
        return None, extra
    # Prefer an exact UPC match, then the first matching NDC padding
    for p in matches:
        if p.upc in upc_codes:
            return p, extra
    order = {ndc: i for i, ndc in enumerate(ndc_candidates)}
    return min(matches, key=lambda p: order.get(p.ndc, 99)), extra
