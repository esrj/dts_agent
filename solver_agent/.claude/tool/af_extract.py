# """
# af_extract.py — Extract the STM32MP25x alternate-function (AF) mapping from
# ST datasheet DS14284 (Table 12: AF0-AF7, Table 13: AF8-AF15) into af_table.json
# for solve.py.

# The AF tables are whitespace-aligned grids (no ruling lines) with two awkward
# properties that defeat naive text extraction:

#   * two-digit pin labels are sometimes split vertically (PA13 -> "PA1" + "3"),
#   * column headers AND data are centre-aligned, but wide cells start far left
#     of the header's x0, so columns must be matched on the CENTRE x-coordinate.

# Strategy (all coordinate-based, via pdfplumber word boxes):
#   1. per sub-table, take the header row -> column centre x for AF0..AF15;
#   2. reconstruct pin labels by merging vertically-stacked fragments in the
#      pin-label x-band;
#   3. assign every data word to the nearest pin-row centre (midpoint bands,
#      so variable row heights are handled) and the nearest column centre;
#   4. concatenate the fragments of each (pin, AFn) cell in reading order;
#   5. split '/'-separated dual names (e.g. SPI3_MISO/I2S3_SDI) into atoms.

# Output preserves the AF number:  { "PF0": {"0": "SPI3_SCK/I2S3_CK", ... } }.

# Run:  python3 af_extract.py            (writes af_table.json + diagnostics)
# """
# import json
# import re
# from collections import defaultdict

# import pdfplumber

# PDF = "DS14284 (1).pdf"
# OUT = "data/af_table.json"
# SCAN_PAGES = range(78, 96)
# PINRE = re.compile(r"^P[A-KZ]\d{1,2}$")
# PRIMRE = re.compile(r"^P[A-KZ]\d")
# AF07 = re.compile(r"^AF[0-7]$")
# AF815 = re.compile(r"^AF(8|9|1[0-5])$")
# # Width (px) of the pin-label x-band. MUST stay narrow (~18): AF0 cells such as
# # TRACEDn/TRACECLK begin only ~26px right of the label and share its row top; a
# # wider band (e.g. 30) pulls them in and they merge into the label
# # ("PD4"+"TRACED0" -> "PD4TRACED0"), silently dropping that pin.
# LABEL_BAND = 18.0
# MERGE_DY = 11.0
# ROW_MARGIN = 0.6
# COL_TOL = 25.0


# def header_centers(words, rx):
#     """Return (header_top, {AFn: centre_x}) for the best matching header row."""
#     cand = [w for w in words if rx.match(w["text"])]
#     if not cand:
#         return None
#     byrow = defaultdict(list)
#     for w in cand:
#         byrow[round(w["top"])].append(w)
#     top = max(byrow, key=lambda t: len({w["text"] for w in byrow[t]}))
#     row = byrow[top]
#     if len({w["text"] for w in row}) < 8:
#         return None
#     cen = {}
#     for w in sorted(row, key=lambda w: w["x0"]):
#         cen.setdefault(w["text"], (w["x0"] + w["x1"]) / 2.0)
#     return top, cen


# def reconstruct_labels(words, pinmin, lo, hi):
#     """Merge vertically-stacked label fragments into full pin names."""
#     band = sorted(
#         (w for w in words
#          if pinmin - 3 <= w["x0"] <= pinmin + LABEL_BAND and lo < w["top"] < hi),
#         key=lambda w: w["top"])
#     merged = []
#     for w in band:
#         if merged and w["top"] - merged[-1]["last"] < MERGE_DY:
#             merged[-1]["text"] += w["text"]
#             merged[-1]["last"] = w["top"]
#         else:
#             merged.append({"text": w["text"], "top": w["top"], "last": w["top"]})
#     anchors = [(m["text"], m["top"]) for m in merged if PINRE.match(m["text"])]
#     anchors.sort(key=lambda a: a[1])
#     return anchors


# def parse_subtable(words, rx, page_h, other_top, stats):
#     """Parse one sub-table -> {pin: {"AFn": cell_text}}."""
#     hc = header_centers(words, rx)
#     if not hc:
#         return {}
#     htop, cen = hc
#     region_hi = other_top if (other_top is not None and other_top > htop) else page_h
#     names = sorted(cen, key=lambda n: cen[n])
#     cx = [cen[n] for n in names]

#     prim = [w for w in words
#             if PRIMRE.match(w["text"]) and htop < w["top"] < region_hi]
#     if not prim:
#         return {}
#     pinmin = min(w["x0"] for w in prim)

#     anchors = reconstruct_labels(words, pinmin, htop, region_hi)
#     if not anchors:
#         return {}
#     atops = [a[1] for a in anchors]
#     if len(atops) > 1:
#         gaps = sorted(atops[i + 1] - atops[i] for i in range(len(atops) - 1))
#         pitch = gaps[len(gaps) // 2]
#     else:
#         pitch = 18.0
#     lo = atops[0] - ROW_MARGIN * pitch
#     hi = atops[-1] + ROW_MARGIN * pitch

#     cells = defaultdict(lambda: defaultdict(list))
#     for w in words:
#         if pinmin - 3 <= w["x0"] <= pinmin + LABEL_BAND:
#             continue
#         if w["text"].strip("-") == "":          # '-' empty-cell markers
#             continue
#         tw = w["top"]
#         if tw < lo or tw > hi:
#             stats["outside"] += 1
#             continue
#         cxw = (w["x0"] + w["x1"]) / 2.0
#         ci = min(range(len(cx)), key=lambda i: abs(cxw - cx[i]))
#         if abs(cxw - cx[ci]) > COL_TOL:
#             stats["farcol"] += 1
#             continue
#         pi = min(range(len(atops)), key=lambda i: abs(tw - atops[i]))
#         cells[anchors[pi][0]][names[ci]].append((tw, w["x0"], w["text"]))

#     out = defaultdict(dict)
#     for pin, afm in cells.items():
#         for afn, frs in afm.items():
#             frs.sort(key=lambda f: (round(f[0]), f[1]))
#             out[pin][afn] = "".join(f[2] for f in frs)
#     return out


# def atoms(cell):
#     return [s for s in (p.strip() for p in cell.split("/")) if s and s != "-"]


# def merge_into(dst, src):
#     for pin, afm in src.items():
#         for afn, txt in afm.items():
#             if txt and txt != "-":
#                 dst[pin][afn] = txt


# def main():
#     pdf = pdfplumber.open(PDF)
#     stats = defaultdict(int)
#     t12 = defaultdict(dict)
#     t13 = defaultdict(dict)

#     for i in SCAN_PAGES:
#         page = pdf.pages[i]
#         words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
#         h7 = header_centers(words, AF07)
#         h815 = header_centers(words, AF815)
#         if h7:
#             merge_into(t12, parse_subtable(words, AF07, page.height,
#                                            h815[0] if h815 else None, stats))
#         if h815:
#             merge_into(t13, parse_subtable(words, AF815, page.height,
#                                            h7[0] if h7 else None, stats))

#     pins12, pins13 = set(t12), set(t13)
#     print("Table 12 pins (AF0-7) : ", len(pins12))
#     print("Table 13 pins (AF8-15): ", len(pins13))
#     print("  only in T12: ", sorted(pins12 - pins13))
#     print("  only in T13: ", sorted(pins13 - pins12))

#     af_num = defaultdict(dict)
#     for pin in pins12 | pins13:
#         af_num[pin].update(t12.get(pin, {}))
#         af_num[pin].update(t13.get(pin, {}))
#     pins = sorted(af_num, key=lambda p: (p[1], int(p[2:])))

#     sig_atomic, sig_raw = set(), set()
#     for pin in pins:
#         for cell in af_num[pin].values():
#             sig_raw.add(cell)
#             sig_atomic.update(atoms(cell))

#     print(f"\n|P|      = {len(pins)}    (paper: 172)")
#     print(f"|Sigma|  atomic (split '/')   = {len(sig_atomic)}    (oracle: 677)")
#     print(f"|Sigma|  raw cells (no split) = {len(sig_raw)}")
#     print(f"filtered-out words: {dict(stats)}")

#     table = {}
#     for pin in pins:
#         table[pin] = {str(n): af_num[pin][f"AF{n}"]
#                       for n in range(16) if f"AF{n}" in af_num[pin]}
#     with open(OUT, "w") as fh:
#         json.dump(table, fh, indent=1)
#     print(f"\nwrote {OUT}")
#     return table


# if __name__ == "__main__":
#     main()






