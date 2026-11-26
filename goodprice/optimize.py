"""
Optimizarea cosului: unde cumperi lista cel mai ieftin.

Model: fiecare produs (canonic) are un pret in unul sau mai multe magazine.
Vrem sa acoperim toata lista minimizand costul total, folosind cel mult
`max_stores` magazine (bataia de cap a opririlor multiple).

Acum enumeram subseturile de magazine (rapid pentru putine magazine). Cand vor
fi 6+ magazine, inlocuim cu OR-Tools (ILP). Interfata ramane aceeasi.
"""
from __future__ import annotations

from itertools import combinations


def _evaluate(items, subset):
    """Coasta si atribuirea pentru un set fix de magazine. None daca vreun produs
    nu exista in niciunul dintre magazinele alese."""
    total = 0.0
    assign = []
    for it in items:
        avail = [(s, it["prices"][s]) for s in subset if it["prices"].get(s) is not None]
        if not avail:
            return None
        store, price = min(avail, key=lambda x: x[1])
        qty = it.get("qty", 1)
        total += price * qty
        assign.append({"item": it["name"], "store": store, "price": price, "qty": qty})
    used = sorted({a["store"] for a in assign})
    return {"total": round(total, 2), "assign": assign, "stores": used, "n_stores": len(used)}


def best_plan(items, stores, max_stores):
    """Cel mai ieftin plan folosind cel mai mult `max_stores` magazine."""
    best = None
    for k in range(1, max_stores + 1):
        for subset in combinations(stores, k):
            cand = _evaluate(items, subset)
            if cand is None:
                continue
            if (best is None
                    or cand["total"] < best["total"] - 1e-9
                    or (abs(cand["total"] - best["total"]) < 1e-9 and cand["n_stores"] < best["n_stores"])):
                best = cand
    return best


def single_store_totals(items, stores):
    """Totalul pe fiecare magazin care are TOATE produsele (pentru afisaj)."""
    res = []
    for s in stores:
        plan = _evaluate(items, (s,))
        if plan is not None:
            res.append({"store": s, "total": plan["total"]})
    return sorted(res, key=lambda x: x["total"])


def optimize(items, stores, max_stores):
    """Rezultat complet: totaluri per magazin + planul optim la limita data."""
    singles = single_store_totals(items, stores)
    plan = best_plan(items, stores, max_stores)
    best_single = singles[0]["total"] if singles else None
    savings = round(best_single - plan["total"], 2) if (plan and best_single is not None) else 0.0
    return {
        "single_store_totals": singles,
        "best_single": best_single,
        "plan": plan,
        "savings_vs_best_single": max(savings, 0.0),
    }
