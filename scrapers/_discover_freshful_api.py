"""One-off: descopera API-ul Freshful (eMAG) + verifica daca expune EAN."""
from __future__ import annotations

import json
import re

from playwright.sync_api import sync_playwright

URLS = [
    "https://www.freshful.ro/",
    "https://www.freshful.ro/c/10-dietetic-bio-si-international/1002-produse-dietetice-si-naturiste/100201-produse-fara-gluten",
    "https://www.freshful.ro/p/100162495-napolact-lapte-3-5-grasime-1-5l",
]
SEEN: dict[str, dict] = {}


def on_response(resp):
    url = resp.url
    rtype = resp.request.resource_type
    if rtype not in ("xhr", "fetch"):
        return
    try:
        body = resp.json()
    except Exception:
        return
    blob = json.dumps(body, ensure_ascii=False)
    looks_product = any(k in blob for k in ('"price"', "pret", '"vendor"', '"part_number"', "measurement"))
    ean_hit = bool(re.search(r'"(ean|barcode|gtin|part_number)"\s*:\s*"?\d{6,}', blob, re.I))
    base = url.split("?")[0]
    if base not in SEEN or looks_product:
        SEEN[base] = {"full": url, "status": resp.status, "product": looks_product,
                      "ean": ean_hit, "sample": blob[:240]}


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            locale="ro-RO")
        page.on("response", on_response)
        for u in URLS:
            try:
                page.goto(u, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"(goto {u}: {type(e).__name__})")
            page.wait_for_timeout(3500)
            for _ in range(4):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(900)
        browser.close()

    print(f"\n=== {len(SEEN)} endpointuri XHR/fetch ===")
    for base, i in sorted(SEEN.items(), key=lambda x: (not x[1]["product"], x[0])):
        tag = "  <PRODUSE" + ("+EAN" if i["ean"] else "") if i["product"] else ""
        print(f"[{i['status']}] {base}{tag}")
    print("\n=== detalii endpointuri cu produse ===")
    for base, i in SEEN.items():
        if i["product"]:
            print(f"\nURL: {i['full']}\n  ean={i['ean']}  sample: {i['sample']}")


if __name__ == "__main__":
    main()
