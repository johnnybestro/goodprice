"""One-off: descopera API-ul GraphQL al Mega Image (operatii + variabile + headere)."""
from __future__ import annotations

import json

from playwright.sync_api import sync_playwright

SEEN: list[dict] = []


def on_response(resp):
    url = resp.url
    if "/api/" not in url or resp.request.resource_type not in ("xhr", "fetch"):
        return
    try:
        body = resp.json()
    except Exception:
        return
    blob = json.dumps(body, ensure_ascii=False)
    req = resp.request
    post = None
    try:
        post = req.post_data
    except Exception:
        pass
    SEEN.append({
        "url": url[:300], "method": req.method, "status": resp.status,
        "product": any(k in blob for k in ('"price"', "priceValue", '"products"', "gtin", '"ean"')),
        "post": (post or "")[:300],
        "sample": blob[:200],
    })


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        pg = b.new_page(user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
                        locale="ro-RO")
        pg.on("response", on_response)
        for u in ["https://www.mega-image.ro/", "https://www.mega-image.ro/search?q=lapte"]:
            try:
                pg.goto(u, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"(goto {u}: {type(e).__name__})")
            pg.wait_for_timeout(4000)
            for _ in range(3):
                pg.mouse.wheel(0, 4000)
                pg.wait_for_timeout(1000)
        b.close()

    print(f"=== {len(SEEN)} raspunsuri API ===")
    for i in SEEN:
        tag = " <PRODUSE" if i["product"] else ""
        print(f"[{i['status']}] {i['method']} {i['url'][:160]}{tag}")
    print("\n=== detalii cu produse ===")
    for i in SEEN:
        if i["product"]:
            print(f"\nURL: {i['url']}")
            if i["post"]:
                print(f"POST: {i['post']}")
            print(f"sample: {i['sample']}")


if __name__ == "__main__":
    main()
