"""
Unealta one-off: descopera endpoint-ul XHR de listare produse al Sezamo.
Incarca o categorie intr-un browser real, da scroll ca sa declanseze lazy-load,
si raporteaza requesturile XHR/fetch care intorc produse.

Ruleaza PE container:  PYTHONPATH=. .venv/bin/python scrapers/_discover_sezamo_api.py
"""
from __future__ import annotations

import json

from playwright.sync_api import sync_playwright

# Vizitam mai multe pagini ca sa surprindem endpointul de produse + paginarea.
URLS = [
    "https://www.sezamo.ro/",                       # homepage: slidere de produse
    "https://www.sezamo.ro/c4089-carne-peste-si-fructe-de-mare",  # categorie larga
]
SEEN: dict[str, dict] = {}


def on_response(resp):
    url = resp.url
    if "/services/" not in url and "/api/" not in url:
        return
    rtype = resp.request.resource_type
    if rtype not in ("xhr", "fetch"):
        return
    try:
        body = resp.json()
    except Exception:
        return
    blob = json.dumps(body)[:200]
    looks_product = any(k in blob for k in ("productId", "cardsData", "originalPrice", '"slug"'))
    SEEN[url.split("?")[0]] = {
        "full_url": url,
        "status": resp.status,
        "looks_product": looks_product,
        "sample": blob,
    }


def main():
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            locale="ro-RO",
        )
        page.on("response", on_response)
        for url in URLS:
            try:
                # goto poate expira (SPA tine conexiuni deschise) — capturarea
                # se face oricum prin on_response, deci continuam.
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"(goto {url}: {type(e).__name__})")
            page.wait_for_timeout(3000)
            for _ in range(6):
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1000)
        browser.close()

    print(f"\n=== {len(SEEN)} endpointuri XHR/fetch unice ===")
    for base, info in sorted(SEEN.items(), key=lambda x: (not x[1]["looks_product"], x[0])):
        flag = "  <-- PRODUSE" if info["looks_product"] else ""
        print(f"[{info['status']}] {base}{flag}")
    print("\n=== detalii endpointuri cu produse ===")
    for base, info in SEEN.items():
        if info["looks_product"]:
            print(f"\nURL: {info['full_url']}")
            print(f"sample: {info['sample']}")


if __name__ == "__main__":
    main()
