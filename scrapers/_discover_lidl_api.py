"""One-off: descopera API-ul Lidl.ro (XHR-urile reale + headerele lor exacte)."""
from __future__ import annotations

import json

from playwright.sync_api import sync_playwright

URLS = [
    "https://www.lidl.ro/q/search?q=lapte",
    "https://www.lidl.ro/c/alimente-bauturi/s10068374",
]
SEEN: dict[str, dict] = {}


def on_response(resp):
    url = resp.url
    if resp.request.resource_type not in ("xhr", "fetch"):
        return
    try:
        body = resp.json()
    except Exception:
        return
    blob = json.dumps(body, ensure_ascii=False)
    product = any(k in blob for k in ('"price"', '"erpNumber"', '"grammage"', '"fullTitle"', "keyfacts"))
    base = url.split("?")[0]
    if base not in SEEN or product:
        # pastram headerele requestului — aici e secretul lui 406
        hdrs = {k: v for k, v in resp.request.headers.items()
                if k.lower() in ("accept", "accept-language", "x-requested-with",
                                 "content-type", "apikey", "x-api-key", "authorization")}
        SEEN[base] = {"full": url[:220], "status": resp.status, "product": product,
                      "req_headers": hdrs, "sample": blob[:200]}


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(headless=True)
        pg = b.new_page(user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
                        locale="ro-RO")
        pg.on("response", on_response)
        for u in URLS:
            try:
                pg.goto(u, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                print(f"(goto: {type(e).__name__})")
            pg.wait_for_timeout(3500)
            # scroll + click pe "more"/"mai multe" daca exista
            for _ in range(4):
                pg.mouse.wheel(0, 4000)
                pg.wait_for_timeout(800)
            for sel in ("button:has-text('mai multe')", "button:has-text('Mai multe')",
                        "[data-testid*='load-more']", "button:has-text('more')"):
                btn = pg.query_selector(sel)
                if btn:
                    try:
                        btn.click()
                        pg.wait_for_timeout(2000)
                    except Exception:
                        pass
                    break
        b.close()

    print(f"\n=== {len(SEEN)} endpointuri ===")
    for base, i in sorted(SEEN.items(), key=lambda x: not x[1]["product"]):
        print(f"[{i['status']}] {'PRODUSE ' if i['product'] else ''}{base}")
    print("\n=== detalii produse ===")
    for base, i in SEEN.items():
        if i["product"]:
            print(f"\nURL: {i['full']}")
            print(f"  headers: {i['req_headers']}")
            print(f"  sample: {i['sample']}")


if __name__ == "__main__":
    main()
