"""Resume por día y por estrategia las operaciones de la simulación en papel.

Uso:
    python resumen_papel.py                          # lee papel_estrategias.csv
    python resumen_papel.py papel_operaciones.csv    # también lee CSV viejos (sin columna estrategia = A)
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text


def leer(rutas: list[Path]) -> list[dict]:
    filas = []
    for ruta in rutas:
        with ruta.open(encoding="utf-8") as archivo:
            for fila in csv.DictReader(archivo):
                fila.setdefault("estrategia", "A")
                fila["estrategia"] = fila["estrategia"] or "A"
                fila["ganancia"] = float(fila["ganancia"])
                filas.append(fila)
    return filas


def agrupar(filas: list[dict]) -> dict[tuple[str, str], dict]:
    """(día, estrategia) -> totales. El día "Total" junta todos los días."""
    grupos: dict[tuple[str, str], dict] = defaultdict(lambda: {"ops": 0, "objetivos": 0, "stops": 0, "ganancia": 0.0})
    for fila in filas:
        for dia in (fila["cerrada"][:10], "Total"):
            g = grupos[(dia, fila["estrategia"])]
            g["ops"] += 1
            g["objetivos"] += fila["motivo"] == "objetivo"
            g["stops"] += fila["motivo"] == "stop"
            g["ganancia"] += fila["ganancia"]
    return dict(grupos)


def tabla(grupos: dict[tuple[str, str], dict]) -> Table:
    t = Table(title="Simulación en papel: resumen", header_style="bold")
    for col in ("Día", "Estrategia", "Operaciones", "Objetivos", "Stops", "Éxito", "Ganancia"):
        t.add_column(col, justify="left" if col in ("Día", "Estrategia") else "right")
    dias = sorted({d for d, _ in grupos if d != "Total"}) + ["Total"]
    for dia in dias:
        for (d, estrategia), g in sorted(grupos.items()):
            if d != dia:
                continue
            ganancia = g["ganancia"]
            t.add_row(
                Text(dia, style="bold" if dia == "Total" else ""), estrategia, str(g["ops"]),
                str(g["objetivos"]), str(g["stops"]), f"{g['objetivos'] / g['ops']:.0%}",
                Text(f"{'+' if ganancia >= 0 else '-'}${abs(ganancia):.2f}", style="green" if ganancia > 0 else "red"),
            )
        if dia != "Total":
            t.add_section()
    return t


def main() -> None:
    parser = argparse.ArgumentParser(description="Resumen de la simulación en papel")
    parser.add_argument("csv", nargs="*", type=Path, default=[Path("papel_estrategias.csv")],
                        help="uno o más CSV (por defecto papel_estrategias.csv)")
    args = parser.parse_args()
    consola = Console()
    faltan = [r for r in args.csv if not r.exists()]
    if faltan:
        consola.print(f"No encontré {', '.join(map(str, faltan))}. Corre primero: python nba_monitor.py --papel")
        return
    filas = leer(args.csv)
    if not filas:
        consola.print("El CSV no tiene operaciones todavía.")
        return
    consola.print(tabla(agrupar(filas)))


if __name__ == "__main__":
    main()
