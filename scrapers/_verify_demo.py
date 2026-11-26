"""Test de demo: flux configurator complet + revizita cu service worker."""
from playwright.sync_api import sync_playwright

URL = "http://localhost:8000"
issues = []

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    pg = b.new_context().new_page()
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)

    pg.goto(URL, wait_until="networkidle", timeout=20000)
    pg.wait_for_timeout(1200)
    pg.reload(wait_until="networkidle")  # revizita cu SW activ
    pg.wait_for_timeout(800)
    if not pg.query_selector("#term"):
        issues.append("revizita: input-ul lipseste (cache stale?)")

    # configureaza 2 termeni
    for term in ["apa", "lapte"]:
        pg.fill("#term", term)
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(2200)
        # alege prima valoare din fiecare fateta pana cand shortlist-ul e activ
        for _ in range(4):
            if not pg.query_selector(".hint:not(.ok)"):
                break
            chip = pg.query_selector(".chip:not(.on)")
            if not chip:
                break
            chip.click()
            pg.wait_for_timeout(400)
        opt = pg.query_selector(".vopt:not(.dis)")
        if not opt:
            issues.append(f"{term}: shortlist gol dupa fatete")
            pg.click("#skipBtn") if pg.query_selector("#skipBtn") else None
            continue
        opt.click()
        pg.wait_for_timeout(300)
        pg.click("#addBtn")
        pg.wait_for_timeout(600)

    n = len(pg.query_selector_all("#list .row"))
    if n < 2:
        issues.append(f"doar {n} linii adaugate")
    pg.click("#goBtn")
    pg.wait_for_timeout(2500)
    total = pg.query_selector(".total")
    if not total:
        issues.append("optimize: fara total")
    else:
        print("total:", total.inner_text())

    pg.reload(wait_until="networkidle")
    pg.wait_for_timeout(1000)
    if len(pg.query_selector_all("#list .row")) < n:
        issues.append("lista nu persista dupa reload")

    print("linii:", n, "| erori consola:", errs or "niciuna")
    print("PROBLEME:", issues or "NICIUNA - demo OK")
    b.close()
