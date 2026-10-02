"""Exporta un periodo de nomina de Toast, una empresa de Gusto por archivo.

Uso:
    python export_payroll.py --start 2026-09-18 --end 2026-10-01

Genera por cada empresa de Gusto:
    staging_<empresa>_<inicio>_<fin>.csv     un renglon por empleado
    exceptions_<inicio>_<fin>.csv            lo que hay que revisar a mano

El staging NO es el CSV de Gusto y no se sube: es el intermedio para cuadrar.
El generador del CSV de Gusto todavia no existe porque falta cerrar dos cosas
(la columna de tips, y el inicio de semana de la cuenta para la columna
workweeks), y porque falta calcular el overtime de California desde los punches
en vez de repetir el de Toast.

Si el periodo trae algo que pagaria mal, el comando sale con codigo distinto de
cero y lo dice. No produce nada "listo" mientras haya un blocker.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import date, datetime

from toast_payroll import ToastClient, aggregate_for_pay_period, write_csv

STAGING_COLUMNS = [
    "gusto_company",
    "gusto_employee_id",
    "last_name",
    "first_name",
    "confianza",
    "locations",
    "shifts",
    "regular_hours",
    "overtime_hours",
    "meal_premium_hours",
    "meal_premium_amount",
    "meal_violation_days",
    "meal_premium_days_sin_tarifa",
    "non_cash_tips",
    "declared_cash_tips",
    "service_charges",
    "email",
    "toast_guids",
]

EXCEPTION_COLUMNS = ["tipo", "empleado", "detalle"]

# Excepciones que no son un aviso: si aparecen, el periodo no esta listo.
BLOCKING_EXCEPTIONS = {
    "Sin identidad en Gusto",
    "Clock-out olvidado",
    "Shift abierto",
    "Double time sin calcular",
    "Prima sin tarifa",
}

# Duracion del periodo de nomina. Cada dos viernes son 14 dias, no 15.
PAY_PERIOD_DAYS = 14


def parse_day(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(f"Fecha invalida: {value}. Usa YYYY-MM-DD.")


def slug(company: str) -> str:
    return (company or "sin-empresa").replace("tacos-franc-", "").replace("-llc", "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Exporta nomina de Toast para Gusto.")
    parser.add_argument("--start", required=True, type=parse_day)
    parser.add_argument("--end", required=True, type=parse_day, help="Ultimo dia, inclusive")
    parser.add_argument("--out-dir", default=".")
    parser.add_argument("--allow-partial-period", action="store_true",
                        help="Permite un rango que no mide un periodo completo")
    args = parser.parse_args()

    if args.end < args.start:
        print("El fin del periodo no puede ser antes del inicio.", file=sys.stderr)
        return 1

    span = (args.end - args.start).days + 1
    if span != PAY_PERIOD_DAYS and not args.allow_partial_period:
        print(f"El rango mide {span} dias y el periodo de nomina son {PAY_PERIOD_DAYS} "
              f"(cada dos viernes).\n"
              f"  Un dia de mas o de menos hace que ese dia se pague dos veces o "
              f"ninguna.\n"
              f"  Si de verdad queres un rango parcial, agrega --allow-partial-period.",
              file=sys.stderr)
        return 2

    if args.end >= date.today():
        print(f"El periodo termina el {args.end} y hoy es {date.today()}: todavia hay "
              f"gente fichando.\n"
              f"  Corre el export cuando el periodo ya cerro, o las horas se mueven "
              f"entre corridas.", file=sys.stderr)
        return 2

    try:
        client = ToastClient.from_env()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1

    print(f"Consultando Toast: {args.start} -> {args.end} ({span} dias)")
    try:
        result = aggregate_for_pay_period(client, args.start, args.end)
    except RuntimeError as error:
        print(f"\n{error}", file=sys.stderr)
        return 3

    os.makedirs(args.out_dir, exist_ok=True)
    stamp = f"{args.start:%Y%m%d}_{args.end:%Y%m%d}"

    written = []
    for company, block in sorted(result["by_company"].items()):
        rows = [r for r in result["rows"] if (r["gusto_company"] or "(sin empresa)") == company]
        path = os.path.join(args.out_dir, f"staging_{slug(company)}_{stamp}.csv")
        write_csv(path, rows, STAGING_COLUMNS)
        written.append((company, path, block))

    exceptions_path = os.path.join(args.out_dir, f"exceptions_{stamp}.csv")
    write_csv(exceptions_path, result["exceptions"], EXCEPTION_COLUMNS)

    for company, path, block in written:
        print()
        print("=" * 64)
        print(f"  {company}")
        print("-" * 64)
        print(f"  Empleados                {block['empleados']:>12}"
              + (f"   ({block['pendientes']} sin identidad)" if block["pendientes"] else ""))
        print(f"  Regular hours            {block['regular_hours']:>12,.2f}")
        print(f"  Overtime hours           {block['overtime_hours']:>12,.2f}")
        print(f"  Meal premium hours       {block['meal_premium_hours']:>12,.2f}")
        print(f"  Meal premium $           {block['meal_premium_amount']:>12,.2f}")
        print(f"  Tips de tarjeta $        {block['non_cash_tips']:>12,.2f}")
        print(f"  Cash tips declarados $   {block['declared_cash_tips']:>12,.2f}")
        print(f"  Service charges $        {block['service_charges']:>12,.2f}")
        print(f"  -> {path}")

    print()
    print("=" * 64)
    counts = Counter(e["tipo"] for e in result["exceptions"])
    blocking = {t: n for t, n in counts.items() if t in BLOCKING_EXCEPTIONS}
    warnings = {t: n for t, n in counts.items() if t not in BLOCKING_EXCEPTIONS}

    if result["excluded"]:
        print(f"  Excluidos de la nomina ({len(result['excluded'])}):")
        for item in result["excluded"][:5]:
            print(f"    {item['nombre']} - {item['motivo']}")

    if warnings:
        print("  Avisos:")
        for tipo, count in sorted(warnings.items(), key=lambda x: -x[1]):
            print(f"    {count:>4}  {tipo}")

    if blocking:
        print()
        print("  NO SUBIR NADA A GUSTO. Bloqueos en este periodo:")
        for tipo, count in sorted(blocking.items(), key=lambda x: -x[1]):
            print(f"    {count:>4}  {tipo}")
        print(f"  Detalle en {exceptions_path}")
        return 4

    print(f"  Sin bloqueos. Excepciones en {exceptions_path}")
    print("  Aun asi el staging no se sube: falta el generador del CSV de Gusto.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
