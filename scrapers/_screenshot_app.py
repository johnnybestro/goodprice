"""Verificare vizuala: configurator (apa -> fatete -> produs -> adauga) + optimizare."""
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    b = pw.chromium.launch(headless=True)
    pg = b.new_page(viewport={"width": 900, "height": 1300})
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto("http://localhost:8000", wait_until="networkidle", timeout=20000)

    # 1) scrie "apa" + Enter -> configurator
    pg.fill("#term", "apa")
    pg.keyboard.press("Enter")
    pg.wait_for_timeout(2500)

    # verifica gating: butonul Adauga trebuie sa fie disabled la inceput
    add_disabled = pg.eval_on_selector("#addBtn", "b => b.disabled")
    print("gating initial (Adauga disabled):", add_disabled)

    # 2) alege fatetele: Cantitate=2 l, Tip=plata
    for val in ["2 l", "plată"]:
        chip = pg.query_selector(f'.chip:text-is("{val}")')
        if chip:
            chip.click()
            pg.wait_for_timeout(400)
        else:
            print(f"! chip lipsa: {val}")

    pg.screenshot(path="/tmp/app_cfg.png", full_page=False)

    # 3) alege primul produs din shortlist si adauga
    opt = pg.query_selector(".vopt:not(.dis)")
    if opt:
        opt.click()
        pg.wait_for_timeout(300)
        pg.click("#addBtn")
        pg.wait_for_timeout(500)
    n = len(pg.query_selector_all("#list .row"))
    print("linii in lista dupa adaugare:", n)

    # 4) optimizeaza
    pg.click("#goBtn")
    pg.wait_for_timeout(2500)
    total = pg.query_selector(".total")
    print("total:", total.inner_text() if total else "LIPSA")
    pg.screenshot(path="/tmp/app.png", full_page=True)
    print("erori consola:", errs or "niciuna")
    b.close()
