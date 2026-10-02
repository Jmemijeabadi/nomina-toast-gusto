"""Propone el mapeo de empleados y de jobs entre Toast y Gusto.

Genera dos CSV que un humano confirma UNA vez:

    employee_map.csv   toast_guid -> gusto_employee_id
    job_map.csv        job de Toast -> title de Gusto

La llave hacia Gusto es gusto_employee_id, no el email: el template de Gusto no
trae email. El nombre solo se usa para PROPONER; nunca para decidir solo.

Uso:
    python build_maps.py
    python build_maps.py --days 30      mas historia, mas empleados cubiertos

Todo lo que no sea inequivoco sale marcado CONFIRMAR. El exportador se niega a
usar una fila marcada CONFIRMAR, asi que nadie cobra por una corazonada.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import os
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from toast_payroll import TOAST_TIMEZONE, ToastClient, resolve_locations

# Cada location de Toast es una EMPRESA distinta en Gusto, con su propio template
# y su propia nomina ("bonita" = Plaza Bonita = National City). El mapeo vive en
# GUSTO_COMPANIES dentro de toast_payroll.py, y las locations se descubren con
# /partners/v1/restaurants en vez de hardcodearse.

EMPLOYEE_MAP_COLUMNS = [
    "toast_guid",
    "toast_location",
    "toast_name",
    "toast_email",
    "toast_phone",
    # person_key es la identidad del SER HUMANO, distinta del gusto_employee_id,
    # que Gusto asigna por empresa. Sin ella, quien ficha en las dos LLCs entra
    # como dos personas al motor de overtime: los umbrales de 8 h/dia, 12 h/dia,
    # 40 h/semana y septimo dia se miden sobre la mitad de sus dias, y el tope de
    # 1 h de prima por dia se aplica dos veces.
    "person_key",
    "persona_confirmada",
    "gusto_employee_id",
    "gusto_name",
    "confianza",
    "accion",
    "nota",
]

JOB_MAP_COLUMNS = [
    "toast_location",
    "toast_job",
    "turnos_en_periodo",
    "gusto_title",
    "confianza",
    "accion",
    "nota",
]

# Cuentas de dispositivo o de sistema que aparecen como "empleado" en Toast y
# fichan turnos, pero no son personas y no se pagan.
NON_PERSON_MARKERS = ("kds", "login", "for all devices", "training", "test account")

# Equivalencias que no se deducen por texto.
JOB_ALIASES = {
    "tortillera": "preparadora de tortilla",
    "tortilla station": "preparadora de tortilla",
    "leader": "Lead",
}


def norm(value: str) -> str:
    """Minusculas, sin acentos, espacios colapsados."""
    decomposed = unicodedata.normalize("NFKD", (value or "").strip().lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(stripped.split())


def name_key(last: str, first: str) -> str:
    return f"{norm(last)} {norm(first)}".strip()


def load_gusto_template(path: str) -> dict:
    """Lee un template de Gusto y agrupa los renglones por empleado.

    Un empleado con varios jobs trae varios renglones con el mismo
    gusto_employee_id; el primario es el que dice "(Primary)" en title.
    """
    if not os.path.exists(path):
        return {}

    employees = {}
    with open(path, newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            employee_id = (row.get("gusto_employee_id") or "").strip()
            if not employee_id:
                continue
            title = (row.get("title") or "").strip()
            record = employees.setdefault(
                employee_id,
                {
                    "gusto_employee_id": employee_id,
                    "last_name": (row.get("last_name") or "").strip(),
                    "first_name": (row.get("first_name") or "").strip(),
                    "titles": [],
                    "primary_title": None,
                },
            )
            record["titles"].append(title)
            if "(Primary)" in title:
                record["primary_title"] = title

    for record in employees.values():
        record["name_key"] = name_key(record["last_name"], record["first_name"])
        record["display"] = f"{record['last_name']}, {record['first_name']}".strip()
        if record["primary_title"] is None and record["titles"]:
            record["primary_title"] = record["titles"][0]
    return employees


def _last_name_matches(toast_last_n: str, gusto_last: str) -> bool:
    """Compara apellidos tolerando apellido compuesto truncado y faltas de dedo.

    Usa tokens completos, NO subcadenas: un apellido corto puede ser subcadena
    de otro mas largo y eso hacia competir a dos personas distintas.
    """
    toast_tokens = set(toast_last_n.split())
    gusto_norm = norm(gusto_last)
    gusto_tokens = set(gusto_norm.split())
    if not toast_tokens or not gusto_tokens:
        return False
    # Un apellido de un token contenido en uno de dos, en cualquier direccion.
    if toast_tokens <= gusto_tokens or gusto_tokens <= toast_tokens:
        return True
    # Variantes ortograficas de una sola letra en el apellido completo.
    if difflib.SequenceMatcher(None, toast_last_n, gusto_norm).ratio() >= 0.90:
        return True
    # Apellido compuesto truncado Y con falta de dedo a la vez: un apellido de
    # un token en Toast contra uno de dos en Gusto, con una letra de diferencia.
    #
    # La regla exige que coincidan TODOS los tokens del apellido mas corto, no
    # uno cualquiera. Con apellidos compuestos mexicanos, aceptar un solo token
    # compuestos, aceptar un solo token hacia que dos apellidos que solo
    # ahi salia un "seguro" apuntando a otra persona.
    corto, largo = ((toast_tokens, gusto_tokens)
                    if len(toast_tokens) <= len(gusto_tokens)
                    else (gusto_tokens, toast_tokens))
    if not corto:
        return False
    return all(
        any(len(c) >= 5 and len(g) >= 5
            and difflib.SequenceMatcher(None, c, g).ratio() >= 0.90
            for g in largo)
        for c in corto
    )


def propose_employee(toast_last: str, toast_firsts, candidates: dict) -> tuple:
    """Propone un gusto_employee_id para un empleado de Toast.

    Devuelve (lista_de_ids, confianza, nota). La confianza es:
      exacto    el nombre completo normalizado coincide
      seguro    el apellido coincide Y el nombre de pila tambien, y hay un solo
                candidato asi
      revisar   hay candidatos pero ninguno con el nombre de pila de acuerdo, o
                hay varios que si
      ninguno   no hay candidato plausible

    Solo exacto y seguro se aceptan sin intervencion humana.

    Toast guarda DOS nombres de pila: firstName (el legal) y chosenName (el que
    usa la persona). Difieren en 38 empleados de esta cuenta, y cada sistema
    expone uno distinto: el reporte de tips usa firstName, la API de empleados
    usa chosenName, y Gusto puede traer cualquiera. Probar los dos resuelve casi
    todos los alias sin preguntar nada. Ejemplos reales de esta cuenta:
      uno tiene el nombre legal en firstName y un apodo en chosenName
      otro tiene el legal correcto y un texto corrupto en chosenName
      otro tiene dos nombres en el legal y solo el segundo en chosenName
    """
    if isinstance(toast_firsts, str):
        toast_firsts = [toast_firsts]
    variants = [v for v in dict.fromkeys(norm(x) for x in toast_firsts if x) if v]
    toast_keys = [name_key(toast_last, v) for v in variants]
    toast_last_n = norm(toast_last)
    first_tokens = {v.split()[0] for v in variants if v}

    exact = [c for c in candidates.values() if c["name_key"] in toast_keys]
    if exact:
        if len(exact) == 1:
            return [exact[0]["gusto_employee_id"]], "exacto", ""
        return ([c["gusto_employee_id"] for c in exact], "revisar",
                f"{len(exact)} empleados en Gusto con el nombre identico")

    safe, loose = [], []
    for candidate in candidates.values():
        if not _last_name_matches(toast_last_n, candidate["last_name"]):
            continue
        gusto_first_n = norm(candidate["first_name"])
        gusto_token = gusto_first_n.split()[0] if gusto_first_n else ""

        # El nombre de pila tiene que coincidir de verdad: igual, o compartiendo
    # el primer token, o una falta de dedo de una letra en el nombre de pila.
    # Dos nombres de pila distintos NO pasan: quedan para revision humana.
        agrees = (
            gusto_first_n in variants
            or (gusto_token and gusto_token in first_tokens)
            or any(difflib.SequenceMatcher(None, v, gusto_first_n).ratio() >= 0.88
                   for v in variants)
        )
        (safe if agrees else loose).append(candidate)

    # Un solo candidato con el nombre de pila de acuerdo gana: los "loose" solo
    # coincidieron en apellido, que es evidencia estrictamente mas debil.
    if len(safe) == 1:
        return ([safe[0]["gusto_employee_id"]], "seguro",
                "apellido y nombre de pila coinciden"
                + (f"; descartados {len(loose)} con solo el apellido parecido" if loose else ""))
    if len(safe) > 1:
        return ([c["gusto_employee_id"] for c in safe], "revisar",
                f"{len(safe)} candidatos con apellido Y nombre de pila parecidos")
    if loose:
        return ([c["gusto_employee_id"] for c in loose], "revisar",
                "apellido parecido pero el nombre de pila NO coincide")

    pool = [c["name_key"] for c in candidates.values()]
    fuzzy = []
    for key in toast_keys:
        fuzzy += difflib.get_close_matches(key, pool, n=2, cutoff=0.78)
    if fuzzy:
        ids = [c["gusto_employee_id"] for c in candidates.values()
               if c["name_key"] in fuzzy]
        return ids, "revisar", "solo parecido difuso del nombre completo"

    return [], "ninguno", "sin candidato en el template de Gusto"


def propose_job(toast_job: str, gusto_titles: set) -> tuple:
    """Propone un title de Gusto para un job de Toast."""
    if not toast_job:
        return "", "ninguno", "el turno no trae jobReference"

    by_norm = {norm(t): t for t in gusto_titles}
    toast_norm = norm(toast_job)

    if toast_norm in by_norm:
        return by_norm[toast_norm], "exacto", ""

    alias = JOB_ALIASES.get(toast_norm)
    if alias and norm(alias) in by_norm:
        return by_norm[norm(alias)], "seguro", f"equivalencia conocida: {toast_job} = {alias}"

    close = difflib.get_close_matches(toast_norm, list(by_norm), n=1, cutoff=0.85)
    if close:
        return by_norm[close[0]], "revisar", "parecido difuso del titulo"

    return "", "ninguno", (
        f"'{toast_job}' no existe como job en Gusto. Crear el job alla, o decidir "
        "a que title mandar esas horas."
    )


def agrupar_personas(registros: list) -> dict:
    """Agrupa registros de Toast que son el MISMO ser humano.

    Une por email real y por telefono, en las dos direcciones (union-find),
    porque los dos datos fallan por separado: hay empleados que comparten el
    email entre locations, y otros con un email distinto en cada una pero el
    mismo telefono. Con un solo criterio se escapa la mitad.

    Devuelve {toast_guid: person_key}.
    """
    padre = {}

    def raiz(x):
        while padre.get(x, x) != x:
            padre[x] = padre.get(padre[x], padre[x])
            x = padre[x]
        return x

    def unir(a, b):
        ra, rb = raiz(a), raiz(b)
        if ra != rb:
            padre[rb] = ra

    por_email = defaultdict(list)
    por_tel = defaultdict(list)
    for registro in registros:
        guid = registro["toast_guid"]
        padre.setdefault(guid, guid)
        email = norm(registro.get("toast_email"))
        telefono = "".join(c for c in (registro.get("toast_phone") or "") if c.isdigit())
        if email:
            por_email[email].append(guid)
        if len(telefono) >= 10:
            por_tel[telefono[-10:]].append(guid)

    for grupo in list(por_email.values()) + list(por_tel.values()):
        for otro in grupo[1:]:
            unir(grupo[0], otro)

    # La llave es el email del grupo si existe, para que sea legible; si no, el
    # guid representante.
    emails_por_raiz = {}
    for registro in registros:
        email = norm(registro.get("toast_email"))
        if email:
            emails_por_raiz.setdefault(raiz(registro["toast_guid"]), email)
    return {r["toast_guid"]: emails_por_raiz.get(raiz(r["toast_guid"]),
                                                 raiz(r["toast_guid"]))
            for r in registros}


def merge_existing(path: str, rows: list, key_fields, carry_fields,
                   compare_field: str | None = None) -> int:
    """Conserva lo que un humano ya confirmo en el archivo anterior.

    Sin esto, cada corrida sobrescribiria el mapeo y habria que volver a
    confirmar los mismos empleados: el archivo es justamente el lugar donde
    queda registrada la decision humana, asi que no se pisa.

    Devuelve cuantas filas se conservaron.
    """
    if not os.path.exists(path):
        return 0
    if isinstance(key_fields, str):
        key_fields = (key_fields,)

    def key_of(row):
        return tuple((row.get(f) or "").strip() for f in key_fields)

    with open(path, newline="", encoding="utf-8-sig") as handle:
        previous = {key_of(r): r for r in csv.DictReader(handle)}

    kept = 0
    for row in rows:
        old_row = previous.get(key_of(row))
        if not old_row:
            continue
        # Solo se respeta lo que ya quedo resuelto: una fila vieja que seguia
        # en CONFIRMAR no debe tapar una propuesta nueva mejor.
        resolved = (old_row.get(carry_fields[0]) or "").strip()
        action = (old_row.get("accion") or "").strip().upper()

        # Si el registro de Toast cambio de nombre, el mapeo viejo ya no es
        # confiable: los restaurantes reusan la cuenta de un empleado que se fue
        # para el que entra, y el guid no cambia. Conservarlo le pagaria al que
        # se fue. Se descarta y se pide confirmar de nuevo.
        if compare_field and resolved:
            antes = norm(old_row.get(compare_field))
            ahora = norm(row.get(compare_field))
            if antes and ahora and antes != ahora:
                row["accion"] = "CONFIRMAR"
                row["nota"] = (f"el registro de Toast cambio de nombre "
                               f"('{old_row.get(compare_field)}' -> "
                               f"'{row.get(compare_field)}'): verificar que siga "
                               f"siendo la misma persona antes de pagarle")
                continue

        if resolved or action == "EXCLUIR":
            for field in carry_fields:
                if field in old_row:
                    row[field] = old_row[field]
            kept += 1
    return kept


def main() -> int:
    parser = argparse.ArgumentParser(description="Propone los mapeos Toast -> Gusto.")
    parser.add_argument("--days", type=int, default=30,
                        help="Dias de historia para descubrir quien trabajo (max 30)")
    parser.add_argument("--out-dir", default=".")
    args = parser.parse_args()

    try:
        client = ToastClient.from_env()
    except RuntimeError as error:
        print(error, file=sys.stderr)
        return 1

    tz = ZoneInfo(TOAST_TIMEZONE)
    end_dt = datetime.combine(date.today() + timedelta(days=1), time.min, tzinfo=tz)
    start_dt = end_dt - timedelta(days=min(args.days, 30))

    employee_rows = []
    job_rows = []
    missing_templates = []
    unconfigured = []

    locations = resolve_locations(client)
    print(f"Toast reporta {len(locations)} location(s) habilitada(s) para esta integracion:")
    for location in locations:
        flag = "" if location["configured"] else "   <-- SIN EMPRESA DE GUSTO"
        print(f"  {location['toast_name']}{flag}")
        if not location["configured"]:
            unconfigured.append(location)
    print()

    for location in locations:
        location_name = location["short"]
        restaurant_guid = location["guid"]
        template_path = location["template"]
        gusto_employees = load_gusto_template(template_path) if template_path else {}
        if not gusto_employees:
            missing_templates.append((location_name, template_path or "(sin template configurado)"))

        gusto_titles = {
            title.replace(" (Primary)", "").strip()
            for record in gusto_employees.values()
            for title in record["titles"]
        }

        toast_employees = {e["guid"]: e for e in client.get_employees(restaurant_guid)}
        jobs = {}
        try:
            for job in client.get_jobs(restaurant_guid):
                jobs[job["guid"]] = job.get("title") or ""
        except Exception as error:
            print(f"  aviso: no se pudieron leer los jobs de {location_name}: {error}",
                  file=sys.stderr)

        worked = set()
        job_usage = Counter()
        for time_entry in client.get_time_entries(restaurant_guid, start_dt, end_dt):
            if time_entry.get("deleted"):
                continue
            worked.add((time_entry.get("employeeReference") or {}).get("guid"))
            job_guid = (time_entry.get("jobReference") or {}).get("guid")
            job_usage[jobs.get(job_guid, "")] += 1

        for toast_guid in sorted(worked, key=lambda g: norm(
                (toast_employees.get(g) or {}).get("lastName") or "")):
            employee = toast_employees.get(toast_guid) or {}
            last = (employee.get("lastName") or "").strip()
            chosen = (employee.get("chosenName") or "").strip()
            legal = (employee.get("firstName") or "").strip()
            first = chosen or legal
            raw_email = (employee.get("email") or "").strip().lower()

            full_norm = norm(f"{last} {first}")
            if any(marker in full_norm for marker in NON_PERSON_MARKERS):
                employee_rows.append({
                    "toast_guid": toast_guid,
                    "toast_location": location_name,
                    "toast_name": f"{last}, {first}".strip(", "),
                    "toast_email": "",
                    "toast_phone": "",
                    "person_key": "",
                    "persona_confirmada": "",
                    "gusto_employee_id": "",
                    "gusto_name": "",
                    "confianza": "no-persona",
                    "accion": "EXCLUIR",
                    "nota": "cuenta de dispositivo o de sistema, no se paga",
                })
                continue

            ids, confidence, note = propose_employee(
                last, [chosen, legal], gusto_employees)
            single = ids[0] if (len(ids) == 1 and confidence in ("exacto", "seguro")) else ""
            names = " | ".join(
                gusto_employees[i]["display"] for i in ids if i in gusto_employees
            )

            employee_rows.append({
                "toast_guid": toast_guid,
                "toast_location": location_name,
                "toast_name": f"{last}, {first}".strip(", "),
                "toast_email": "" if "@example.com" in raw_email else raw_email,
                "toast_phone": (employee.get("phoneNumber") or "").strip(),
                "person_key": "",
                "persona_confirmada": "",
                "gusto_employee_id": single,
                "gusto_name": names,
                "confianza": confidence,
                "accion": "" if single else "CONFIRMAR",
                "nota": note,
            })

        for toast_job, count in job_usage.most_common():
            title, confidence, note = propose_job(toast_job, gusto_titles)
            job_rows.append({
                "toast_location": location_name,
                "toast_job": toast_job or "(sin jobReference)",
                "turnos_en_periodo": count,
                "gusto_title": title,
                "confianza": confidence,
                "accion": "" if title else "CONFIRMAR",
                "nota": note,
            })

    # Identidad de PERSONA: agrupa los registros de Toast del mismo ser humano.
    pagables = [r for r in employee_rows if r["confianza"] != "no-persona"]
    claves = agrupar_personas(pagables)
    for row in employee_rows:
        row["person_key"] = claves.get(row["toast_guid"], row["toast_guid"])

    # Un grupo que abarca dos empresas de Gusto decide si las horas se suman
    # para el overtime. Eso es una decision de joint employer, no un detalle
    # tecnico: se propone y un humano la confirma. Mientras no este confirmada,
    # el exportador NO suma entre empresas.
    por_persona = defaultdict(list)
    for row in pagables:
        por_persona[row["person_key"]].append(row)
    cruzados = {k: v for k, v in por_persona.items()
                if len({r["toast_location"] for r in v}) > 1}
    for clave, grupo in cruzados.items():
        for row in grupo:
            if not row["persona_confirmada"]:
                row["persona_confirmada"] = "CONFIRMAR"

    os.makedirs(args.out_dir, exist_ok=True)
    employee_path = os.path.join(args.out_dir, "employee_map.csv")
    job_path = os.path.join(args.out_dir, "job_map.csv")

    kept = merge_existing(employee_path, employee_rows, "toast_guid",
                          ("gusto_employee_id", "accion", "nota",
                           "person_key", "persona_confirmada"),
                          compare_field="toast_name")
    kept += merge_existing(job_path, job_rows, ("toast_location", "toast_job"),
                           ("gusto_title", "accion", "nota"))

    for path, rows, columns in ((employee_path, employee_rows, EMPLOYEE_MAP_COLUMNS),
                                (job_path, job_rows, JOB_MAP_COLUMNS)):
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    if kept:
        print(f"Conservadas {kept} fila(s) ya resueltas de los mapeos anteriores.")
        print()
    by_confidence = Counter(r["confianza"] for r in employee_rows)
    pending = [r for r in employee_rows if r["accion"]]

    print("=" * 64)
    print("  MAPEO DE EMPLEADOS")
    for level in ("exacto", "seguro", "revisar", "ninguno"):
        if by_confidence.get(level):
            mark = "  " if level in ("exacto", "seguro") else "->"
            print(f"  {mark} {level:<9} {by_confidence[level]:>3}")
    print(f"  Listos sin tocar: {len(employee_rows) - len(pending)} de {len(employee_rows)}")
    print("-" * 64)
    print("  MAPEO DE JOBS")
    for row in job_rows:
        if row["accion"]:
            print(f"  -> {row['toast_location'][:14]:<14} {row['toast_job'][:32]:<32} "
                  f"{row['turnos_en_periodo']:>4} turnos  SIN TITLE")
    print("=" * 64)
    print(f"  {employee_path}")
    print(f"  {job_path}")

    if unconfigured:
        print()
        print("  Locations que Toast reporta pero que NO tienen empresa de Gusto mapeada:")
        for location in unconfigured:
            print(f"    {location['toast_name']}  guid={location['guid']}")
        print("    Agregalas a GUSTO_COMPANIES en toast_payroll.py o sus horas quedan fuera.")

    if missing_templates:
        print()
        print("  Falta el template de Gusto de:")
        for location_name, path in missing_templates:
            print(f"    {location_name}  ->  descargalo y guardalo como {path}")

    if cruzados:
        print()
        print(f"  {len(cruzados)} persona(s) trabajan en las DOS empresas de Gusto.")
        print("  Eso decide si sus horas se SUMAN para el overtime de California.")
        print("  Mientras 'persona_confirmada' diga CONFIRMAR, el exportador NO las suma:")
        for clave, grupo in cruzados.items():
            print(f"    {grupo[0]['toast_name']}  ({clave})")
            for row in grupo:
                print(f"       {row['toast_location'][:14]:<14} gusto_id={row['gusto_employee_id'] or '(sin mapear)':<10}"
                      f" tel={row['toast_phone'] or '-'}  {row['toast_email'] or '(sin email)'}")
        print("  Si es la misma persona, pon 'si' en persona_confirmada en las dos filas.")

    if pending:
        print()
        print(f"  {len(pending)} empleados necesitan tu confirmacion. Abri employee_map.csv,")
        print("  llena gusto_employee_id y borra el CONFIRMAR de la columna accion:")
        for row in pending[:15]:
            print(f"    {row['toast_location'][:13]:<13} {row['toast_name'][:30]:<30} "
                  f"[{row['confianza']}] {row['nota'][:44]}")
            if row["gusto_name"]:
                print(f"                  candidato(s): {row['gusto_name'][:70]}")
        if len(pending) > 15:
            print(f"    ... y {len(pending) - 15} mas en el archivo")

    return 0


if __name__ == "__main__":
    sys.exit(main())
