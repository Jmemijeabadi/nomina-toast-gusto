"""Mide si los tips post-pool se pueden derivar de la API de Toast.

La pregunta: se puede dejar de bajar el reporte EmployeeTipTotals a mano?

Toast NO expone la distribucion del pool (8 endpoints probados, todos 404) ni
hay campos de tips en la Analytics API. Pero la Orders API si esta abierta y
tiene los insumos. Esto mide, en dos pasos:

Este archivo mide SOLO el bruto: los tips que las ordenes reportan contra el
"antes del pool" del reporte. Si el bruto no cuadra, el reparto no se puede
reconstruir y no tiene sentido seguir.

El segundo paso, comparar el reparto calculado persona por persona contra el
"despues del pool", NO esta aqui: se corre con tip_pool.repartir_periodo contra
el reporte. Su resultado medido esta en el encabezado de tip_pool.py.

Uso:
    python verificar_tips.py
"""

from __future__ import annotations

import collections
import csv
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import tip_pool
from toast_payroll import ToastClient, resolve_locations

PERIODOS = [
    (date(2026, 8, 22), date(2026, 9, 4)),
    (date(2026, 9, 5), date(2026, 9, 18)),
]
REPORTES = {
    "National City": "reference/tips_NC_{inicio}_{fin}.csv",
    "Gaslamp Quarter": "reference/tips_GL_{inicio}_{fin}.csv",
}


def money(texto: str) -> float:
    limpio = re.sub(r"[^0-9.\-]", "", texto or "")
    try:
        return float(limpio) if limpio not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def extraer_dia(orders: list) -> dict:
    """Tips y ventas por mesero de un dia, con las dos correcciones que faltaban.

    - suma la propina automatica, que viene como appliedServiceCharges con
      gratuity=true y NO como tipAmount
    - descarta los pagos que no quedaron CAPTURED (VOIDED, DENIED)
    """
    tips = collections.defaultdict(float)
    ventas = collections.defaultdict(float)
    descartado = 0.0
    sin_mesero = 0.0

    for orden in orders:
        if orden.get("deleted"):
            continue
        mesero = (orden.get("server") or {}).get("guid")
        for check in (orden.get("checks") or []):
            if check.get("deleted"):
                continue
            monto = 0.0
            for pago in (check.get("payments") or []):
                propina = float(pago.get("tipAmount") or 0)
                if pago.get("deleted") or pago.get("paymentStatus") != "CAPTURED":
                    descartado += propina
                else:
                    monto += propina
            for cargo in (check.get("appliedServiceCharges") or []):
                if cargo.get("gratuity") and not cargo.get("deleted"):
                    monto += float(cargo.get("chargeAmount") or 0)
            if mesero:
                tips[mesero] += monto
            else:
                sin_mesero += monto
            for sel in (check.get("selections") or []):
                if sel.get("deleted"):
                    continue
                if (sel.get("salesCategory") or {}).get("guid") in tip_pool.SALES_CATEGORIES:
                    if mesero:
                        ventas[mesero] += float(sel.get("price") or 0)

    return {"tips": dict(tips), "ventas": dict(ventas),
            "descartado": descartado, "sin_mesero": sin_mesero}


def traer_periodo(client: ToastClient, guid: str, inicio: date, fin: date) -> dict:
    """Trae los dias en paralelo. En serie esto tardaba mas de 25 minutos."""
    dias = []
    dia = inicio
    while dia <= fin:
        dias.append(dia)
        dia += timedelta(days=1)

    def uno(d):
        return d, extraer_dia(tip_pool.fetch_orders(client, guid, d))

    with ThreadPoolExecutor(max_workers=3) as pool:
        return dict(pool.map(uno, dias))


def bruto_del_reporte(ruta: str) -> tuple:
    """(bruto antes del pool, total despues del pool) del EmployeeTipTotals."""
    antes = despues = 0.0
    with open(ruta, newline="", encoding="utf-8-sig") as handle:
        for fila in csv.DictReader(handle):
            if "online ordering" in (fila.get("Employee") or "").lower():
                continue
            antes += money(fila.get("Tips and gratuity before pooling"))
            despues += money(fila.get("Tips and gratuity after pooling"))
    return antes, despues


def main() -> int:
    client = ToastClient.from_env()
    client.token  # precalentar antes de los hilos
    locations = {loc["short"]: loc for loc in resolve_locations(client)}

    print("=" * 74, flush=True)
    print("  PASO 1: el bruto de las ordenes contra el 'antes del pool' del reporte")
    print("=" * 74, flush=True)

    todo = {}
    for inicio, fin in PERIODOS:
        for short, plantilla in REPORTES.items():
            ruta = plantilla.format(inicio=inicio, fin=fin)
            try:
                antes, despues = bruto_del_reporte(ruta)
            except FileNotFoundError:
                print(f"  (sin reporte: {ruta})")
                continue

            print(f"\n  bajando {short} {inicio} a {fin} ...", flush=True)
            dias = traer_periodo(client, locations[short]["guid"], inicio, fin)
            todo[(inicio, fin, short)] = dias
            ordenes = sum(sum(d["tips"].values()) + d["sin_mesero"] for d in dias.values())
            descartado = sum(d["descartado"] for d in dias.values())

            hueco = ordenes - antes
            pct = (100 * hueco / antes) if antes else 0
            print(f"\n  {short}  {inicio} a {fin}")
            print(f"     ordenes (tipAmount CAPTURED + gratuity) ${ordenes:>12,.2f}")
            print(f"     reporte, antes del pool                 ${antes:>12,.2f}")
            print(f"     hueco                                   ${hueco:>+12,.2f}  ({pct:+.2f}%)")
            print(f"     descartado por VOIDED/DENIED            ${descartado:>12,.2f}")
            print(f"     (el reporte reparte ${despues:,.2f} despues del pool)")

    print()
    print("=" * 74, flush=True)
    print("  Si el hueco del paso 1 no es casi cero, el reparto no se puede")
    print("  reconstruir y hay que seguir bajando el reporte.")
    print("=" * 74, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
