"""Pone, revisa y cancela tus órdenes límite en Polymarket desde la consola.

Pensado para la estrategia de recompensas de liquidez: órdenes de compra que esperan en el
libro cerca del precio medio. Todas las órdenes van con post-only, así que si tu precio
compraría al momento Polymarket la rechaza: siempre eres maker y nunca pagas comisión.

Antes de mandar una orden te muestra el libro, a qué distancia queda del precio medio y si
cuenta para el premio, y te pide confirmar. Solo acepta mercados de deportes.

Llaves: las lee del archivo .env de esta carpeta (no se sube a GitHub):
    SIGNER_PRIVATE_KEY=0x...          llave privada del firmante
    POLYMARKET_WALLET_ADDRESS=0x...   dirección de tu wallet (la de Depositar)
    MAX_USD_POR_ORDEN=15              tope de USD por orden

Uso:
    python ordenes.py poner <mercado> --lado no --precio 0.22 --acciones 50
    python ordenes.py poner <mercado> --lado no --precio 0.22 --acciones 50 --prueba   # revisa sin mandar
    python ordenes.py ver                       # tus órdenes abiertas y si cuentan para el premio
    python ordenes.py cancelar <id>             # basta con el principio del id
    python ordenes.py cancelar-todo
    python ordenes.py ganancias                 # premio cobrado ayer y tu porcentaje actual

<mercado> es el link del mercado en polymarket.com, su slug o su condition id (0x...).
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse

import requests
from rich.console import Console
from rich.table import Table

from nba_monitor import CLOB_URL, GAMMA_URL, _fecha, _json_list
from recompensas import RANGO_UN_LADO, es_deporte, etiquetas_deportes, precios_del_libro

ARCHIVO_ENV = Path(__file__).with_name(".env")
MAX_USD_DEFECTO = 15.0
MIN_HORAS_ANTES = 1.0
SINONIMOS = {"si": "yes", "sí": "yes"}

consola = Console()


class ErrorOrden(Exception):
    """Algo que el usuario tiene que corregir; se muestra sin traza."""


def cargar_env(ruta: Path = ARCHIVO_ENV) -> dict[str, str]:
    """Lee KEY=VALOR del .env. Las variables del sistema tienen prioridad."""
    valores: dict[str, str] = {}
    if ruta.exists():
        for linea in ruta.read_text(encoding="utf-8-sig").splitlines():
            linea = linea.strip()
            if not linea or linea.startswith("#") or "=" not in linea:
                continue
            clave, valor = linea.split("=", 1)
            valores[clave.strip()] = valor.strip().strip('"').strip("'")
    for clave in list(valores):
        if os.environ.get(clave):
            valores[clave] = os.environ[clave]
    return valores


# --- Mercado ------------------------------------------------------------------------------

def _gamma(sesion: requests.Session, ruta: str, params: dict) -> list[dict]:
    r = sesion.get(f"{GAMMA_URL}/{ruta}", params=params, timeout=15)
    r.raise_for_status()
    datos = r.json()
    return datos if isinstance(datos, list) else [datos]


def _mercados_de_evento(evento: dict) -> list[dict]:
    # Los mercados dentro de un evento no traen el evento: se agrega para leer sus etiquetas.
    return [{**m, "events": [evento]} for m in evento.get("markets") or []]


def _elegir(mercados: list[dict], texto: str) -> dict:
    abiertos = [m for m in mercados if not m.get("closed")]
    if len(abiertos) == 1:
        return abiertos[0]
    if not abiertos:
        raise ErrorOrden(f"No encontré mercados abiertos para «{texto}».")
    lista = "\n".join(f"  {m.get('slug')}   ({m.get('groupItemTitle') or m.get('question')})" for m in abiertos)
    raise ErrorOrden(f"Ese evento tiene varios mercados. Usa el slug del que quieres:\n{lista}")


def buscar_mercado(sesion: requests.Session, texto: str) -> dict:
    """Acepta el link de polymarket.com, el slug del mercado o del evento, o el condition id."""
    texto = texto.strip()
    if texto.startswith("0x"):
        mercados = _gamma(sesion, "markets", {"condition_ids": texto, "include_tag": "true"})
        if mercados:
            return mercados[0]
        raise ErrorOrden(f"No encontré el mercado {texto}.")
    partes = [p for p in urlparse(texto).path.split("/") if p] if "polymarket.com" in texto else [texto]
    if partes and partes[0] in ("event", "market", "sports"):
        partes = partes[1:]
    if not partes:
        raise ErrorOrden(f"No entiendo «{texto}». Pega el link del mercado.")
    slug_evento, slug_mercado = partes[0], (partes[1] if len(partes) > 1 else partes[0])
    mercados = _gamma(sesion, "markets", {"slug": slug_mercado, "include_tag": "true"})
    if mercados:
        m = mercados[0]
        if not m.get("events") or not any(e.get("tags") for e in m["events"]):
            eventos = _gamma(sesion, "events", {"slug": slug_evento})
            if eventos:
                m = {**m, "events": eventos}
        return m
    eventos = _gamma(sesion, "events", {"slug": slug_evento})
    if not eventos:
        raise ErrorOrden(f"No encontré «{texto}» en Polymarket.")
    return _elegir(_mercados_de_evento(eventos[0]), texto)


def token_del_lado(mercado: dict, lado: str) -> tuple[str, str]:
    resultados = [str(r) for r in _json_list(mercado.get("outcomes"))]
    tokens = [str(t) for t in _json_list(mercado.get("clobTokenIds"))]
    buscado = SINONIMOS.get(lado.strip().lower(), lado.strip().lower())
    for resultado, token in zip(resultados, tokens):
        if resultado.lower() == buscado:
            return resultado, token
    raise ErrorOrden(f"Este mercado no tiene el lado «{lado}». Opciones: {', '.join(resultados)}.")


def leer_libro(sesion: requests.Session, token_id: str) -> dict:
    r = sesion.get(f"{CLOB_URL}/book", params={"token_id": token_id}, timeout=15)
    r.raise_for_status()
    return r.json()


# --- Revisión antes de mandar ---------------------------------------------------------------

@dataclass
class Revision:
    pregunta: str
    resultado: str
    token_id: str
    precio: float
    acciones: float
    costo: float
    mejor_compra: float | None
    mejor_venta: float | None
    medio: float | None
    spread: float | None          # centavos
    distancia: float | None       # centavos entre tu precio y el medio
    min_acciones: float | None    # del premio
    max_distancia: float | None   # del premio, centavos
    bloqueos: list[str] = field(default_factory=list)   # no se manda
    avisos: list[str] = field(default_factory=list)     # se puede mandar, pero no cuenta o es riesgoso

    @property
    def cuenta_para_premio(self) -> bool:
        return not self.bloqueos and not any(a.startswith("No cuenta") for a in self.avisos)


def _num(valor) -> float | None:
    try:
        return None if valor in (None, "") else float(valor)
    except (TypeError, ValueError):
        return None


def revisar(mercado: dict, libro: dict, resultado: str, token_id: str, precio: float, acciones: float,
            max_usd: float, ids_deportes: set[str], ahora: datetime) -> Revision:
    compras, ventas = precios_del_libro(libro)
    mejor_compra = compras[0][0] if compras else None
    mejor_venta = ventas[0][0] if ventas else None
    medio = (mejor_compra + mejor_venta) / 2 if compras and ventas else None
    rev = Revision(
        pregunta=mercado.get("question") or "?", resultado=resultado, token_id=token_id,
        precio=precio, acciones=acciones, costo=precio * acciones,
        mejor_compra=mejor_compra, mejor_venta=mejor_venta, medio=medio,
        spread=(mejor_venta - mejor_compra) * 100 if medio is not None else None,
        distancia=abs(medio - precio) * 100 if medio is not None else None,
        min_acciones=_num(mercado.get("rewardsMinSize")), max_distancia=_num(mercado.get("rewardsMaxSpread")),
    )
    tick = float(libro.get("tick_size") or 0.01)
    min_orden = _num(libro.get("min_order_size")) or 0

    if not es_deporte(mercado, ids_deportes):
        rev.bloqueos.append("No es un mercado de deportes.")
    if mercado.get("closed") or mercado.get("acceptingOrders") is False:
        rev.bloqueos.append("El mercado no acepta órdenes.")
    if not 0 < precio < 1:
        rev.bloqueos.append("El precio tiene que estar entre 0 y 1.")
    elif abs(round(precio / tick) * tick - precio) > 1e-9:
        rev.bloqueos.append(f"El precio tiene que ir en pasos de {tick:g}.")
    if acciones <= 0 or acciones < min_orden:
        rev.bloqueos.append(f"El mínimo de Polymarket para este mercado es {min_orden:g} acciones.")
    if rev.costo > max_usd + 1e-9:
        rev.bloqueos.append(f"Cuesta ${rev.costo:.2f} y tu tope es ${max_usd:g} (MAX_USD_POR_ORDEN).")
    if mejor_venta is not None and precio >= mejor_venta:
        rev.bloqueos.append(f"Compraría al momento (alguien vende a {mejor_venta:g}). Pon un precio menor.")

    inicio = _fecha(mercado.get("gameStartTime"))
    if inicio and (inicio - ahora).total_seconds() < MIN_HORAS_ANTES * 3600:
        rev.avisos.append("El partido empieza en menos de 1 hora o ya empezó: el precio puede brincar.")
    if not rev.max_distancia or not rev.min_acciones:
        rev.avisos.append("No cuenta: este mercado no tiene premio de liquidez.")
    elif medio is None:
        rev.avisos.append("No cuenta: el libro tiene un solo lado y no hay precio medio real.")
    else:
        if rev.distancia > rev.max_distancia:
            rev.avisos.append(f"No cuenta: queda a {rev.distancia:.1f}¢ del medio y el máximo es {rev.max_distancia:g}¢.")
        if acciones < rev.min_acciones:
            rev.avisos.append(f"No cuenta: el premio pide al menos {rev.min_acciones:g} acciones.")
        if not RANGO_UN_LADO[0] <= medio <= RANGO_UN_LADO[1]:
            rev.avisos.append("No cuenta solo de un lado: el medio está fuera de 0.10-0.90.")
        if rev.spread > rev.max_distancia:
            rev.avisos.append(f"El spread es {rev.spread:.1f}¢: el precio medio puede ser falso y moverse con tu orden.")
    return rev


def mostrar_revision(rev: Revision) -> None:
    t = Table(title=rev.pregunta, show_header=False, title_justify="left")
    t.add_column(style="bold")
    t.add_column()
    fmt = lambda v, f="{:.3f}": "—" if v is None else f.format(v)
    t.add_row("Orden", f"Comprar {rev.resultado} · {rev.acciones:g} acciones a {rev.precio:g} = ${rev.costo:.2f}")
    t.add_row("Libro de " + rev.resultado, f"compra {fmt(rev.mejor_compra)} · venta {fmt(rev.mejor_venta)} · medio {fmt(rev.medio)}")
    t.add_row("Spread", fmt(rev.spread, "{:.1f}¢"))
    t.add_row("Tu distancia al medio", fmt(rev.distancia, "{:.1f}¢"))
    t.add_row("Premio", "sin premio" if not rev.max_distancia else
              f"mínimo {rev.min_acciones:g} acciones · máximo {rev.max_distancia:g}¢ del medio")
    t.add_row("¿Cuenta para el premio?", "[green]Sí[/green]" if rev.cuenta_para_premio else "[red]No[/red]")
    consola.print(t)
    for b in rev.bloqueos:
        consola.print(f"[bold red]✗ {b}[/bold red]")
    for a in rev.avisos:
        consola.print(f"[yellow]! {a}[/yellow]")


# --- Cliente de Polymarket ----------------------------------------------------------------

def conectar(env: dict[str, str]):
    llave = env.get("SIGNER_PRIVATE_KEY")
    if not llave:
        raise ErrorOrden(f"Falta SIGNER_PRIVATE_KEY en {ARCHIVO_ENV}.")
    try:
        from polymarket import SecureClient
    except ImportError as e:
        raise ErrorOrden("Falta la librería de Polymarket: corre  pip install -r requirements.txt") from e
    with consola.status("Conectando con Polymarket..."):
        return SecureClient.create(private_key=llave, wallet=env.get("POLYMARKET_WALLET_ADDRESS") or None)


def _todas(paginador) -> list:
    return [item for pagina in paginador for item in pagina.items]


def preguntas_por_condicion(sesion: requests.Session, condition_ids: list[str]) -> dict[str, str]:
    preguntas: dict[str, str] = {}
    for i in range(0, len(condition_ids), 50):
        bloque = condition_ids[i:i + 50]
        try:
            for m in _gamma(sesion, "markets", {"condition_ids": bloque, "limit": len(bloque)}):
                preguntas[str(m.get("conditionId"))] = m.get("question") or "?"
        except requests.RequestException:
            pass
    return preguntas


# --- Comandos -----------------------------------------------------------------------------

def cmd_poner(args, env: dict[str, str], sesion: requests.Session, cliente=None) -> None:
    max_usd = float(env.get("MAX_USD_POR_ORDEN") or MAX_USD_DEFECTO)
    mercado = buscar_mercado(sesion, args.mercado)
    resultado, token_id = token_del_lado(mercado, args.lado)
    rev = revisar(mercado, leer_libro(sesion, token_id), resultado, token_id, args.precio, args.acciones,
                  max_usd, etiquetas_deportes(sesion), datetime.now(timezone.utc))
    mostrar_revision(rev)
    if rev.bloqueos:
        raise ErrorOrden("No mandé la orden.")
    if args.prueba:
        consola.print("[dim]Modo prueba: no se mandó nada.[/dim]")
        return
    if not args.si and input("¿Mandar la orden? (s/n) ").strip().lower() not in ("s", "si", "sí"):
        consola.print("Cancelado, no se mandó nada.")
        return
    cliente = cliente or conectar(env)
    resp = cliente.place_limit_order(token_id=token_id, side="BUY", price=str(args.precio),
                                     size=str(args.acciones), post_only=True)
    if not resp.ok:
        raise ErrorOrden(f"Polymarket rechazó la orden: {resp.message} ({resp.code})")
    consola.print(f"[bold green]✓ Orden puesta[/bold green] · estado {resp.status} · id {resp.order_id}")
    consola.print("[dim]Revisa en unos minutos con  python ordenes.py ver  si cuenta para el premio.[/dim]")


def cmd_ver(args, env: dict[str, str], sesion: requests.Session, cliente=None) -> None:
    cliente = cliente or conectar(env)
    ordenes = _todas(cliente.list_open_orders())
    if not ordenes:
        consola.print("No tienes órdenes abiertas.")
        return
    puntuan = cliente.get_orders_scoring(order_ids=[o.id for o in ordenes])
    preguntas = preguntas_por_condicion(sesion, sorted({str(o.condition_id) for o in ordenes}))
    t = Table(title="Tus órdenes abiertas", header_style="bold")
    for col in ("Id", "Mercado", "Lado", "Precio", "Acciones", "Llenadas", "Costo", "¿Cuenta?"):
        t.add_column(col, justify="left" if col in ("Id", "Mercado", "Lado") else "right")
    for o in ordenes:
        t.add_row(o.id[:10], preguntas.get(str(o.condition_id), str(o.condition_id)[:10]),
                  f"{o.side} {o.outcome}", f"{o.price}", f"{o.original_size}", f"{o.size_matched}",
                  f"${o.price * (o.original_size - o.size_matched):.2f}",
                  "[green]Sí[/green]" if puntuan.get(o.id) else "[red]No[/red]")
    consola.print(t)
    consola.print("[dim]Para cancelar: python ordenes.py cancelar <id> (basta con el principio del id).[/dim]")


def _mostrar_cancelacion(resp) -> None:
    for oid in resp.canceled:
        consola.print(f"[green]✓ Cancelada {oid}[/green]")
    for oid, razon in resp.not_canceled.items():
        consola.print(f"[red]✗ No se canceló {oid}: {razon}[/red]")
    if not resp.canceled and not resp.not_canceled:
        consola.print("No había órdenes que cancelar.")


def cmd_cancelar(args, env: dict[str, str], sesion: requests.Session, cliente=None) -> None:
    cliente = cliente or conectar(env)
    abiertas = [o.id for o in _todas(cliente.list_open_orders())]
    ids = []
    for prefijo in args.ids:
        coinciden = [oid for oid in abiertas if oid.lower().startswith(prefijo.lower())]
        if len(coinciden) != 1:
            raise ErrorOrden(f"«{prefijo}» coincide con {len(coinciden)} órdenes abiertas. Usa más caracteres del id.")
        ids.append(coinciden[0])
    _mostrar_cancelacion(cliente.cancel_orders(order_ids=ids))


def cmd_cancelar_todo(args, env: dict[str, str], sesion: requests.Session, cliente=None) -> None:
    if not args.si and input("¿Cancelar TODAS tus órdenes abiertas? (s/n) ").strip().lower() not in ("s", "si", "sí"):
        consola.print("No se canceló nada.")
        return
    cliente = cliente or conectar(env)
    _mostrar_cancelacion(cliente.cancel_all())


def cmd_ganancias(args, env: dict[str, str], sesion: requests.Session, cliente=None) -> None:
    cliente = cliente or conectar(env)
    fecha = args.fecha or (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    totales = cliente.get_total_earnings_for_user_for_day(date=fecha, sponsored=True)
    total = sum((Decimal(t.earnings) for t in totales), Decimal(0))
    consola.print(f"Premio del {fecha} (UTC): [bold]${total:.2f}[/bold]"
                  + ("" if totales else " [dim](sin registros ese día)[/dim]"))
    porcentajes = cliente.get_reward_percentages()
    if not porcentajes:
        consola.print("Ahora no tienes órdenes que cuenten para el premio.")
        return
    preguntas = preguntas_por_condicion(sesion, list(porcentajes))
    t = Table(title="Tu parte de la bolsa ahora", header_style="bold")
    t.add_column("Mercado")
    t.add_column("Tu parte", justify="right")
    for cid, pct in sorted(porcentajes.items(), key=lambda x: -x[1]):
        t.add_row(preguntas.get(str(cid), str(cid)[:10]), f"{pct:.1f}%")
    consola.print(t)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Pone, revisa y cancela tus órdenes límite en Polymarket")
    sub = parser.add_subparsers(dest="comando", required=True)
    p = sub.add_parser("poner", help="poner una orden de compra post-only (GTC)")
    p.add_argument("mercado", help="link de polymarket.com, slug o condition id")
    p.add_argument("--lado", required=True, help="resultado a comprar: yes, no o el nombre del equipo")
    p.add_argument("--precio", type=float, required=True, help="precio límite, por ejemplo 0.22")
    p.add_argument("--acciones", type=float, required=True, help="número de acciones")
    p.add_argument("--prueba", action="store_true", help="solo revisar, no mandar")
    p.add_argument("--si", action="store_true", help="no pedir confirmación")
    sub.add_parser("ver", help="ver tus órdenes abiertas")
    c = sub.add_parser("cancelar", help="cancelar órdenes por id")
    c.add_argument("ids", nargs="+", help="id o principio del id")
    ct = sub.add_parser("cancelar-todo", help="cancelar todas tus órdenes abiertas")
    ct.add_argument("--si", action="store_true", help="no pedir confirmación")
    g = sub.add_parser("ganancias", help="premio cobrado y tu parte actual")
    g.add_argument("--fecha", help="día en formato AAAA-MM-DD (por defecto ayer, UTC)")
    args = parser.parse_args(argv)

    comandos = {"poner": cmd_poner, "ver": cmd_ver, "cancelar": cmd_cancelar,
                "cancelar-todo": cmd_cancelar_todo, "ganancias": cmd_ganancias}
    try:
        comandos[args.comando](args, cargar_env(), requests.Session())
    except ErrorOrden as e:
        consola.print(f"[red]{e}[/red]")
        raise SystemExit(1)
    except requests.RequestException as e:
        consola.print(f"[red]No pude conectarme con Polymarket: {e}[/red]")
        raise SystemExit(1)
    except Exception as e:
        if not type(e).__module__.startswith("polymarket"):
            raise
        # Errores de la librería (llave inválida, orden rechazada, sin saldo...): sin traza.
        consola.print(f"[red]Polymarket respondió con un error: {e}[/red]")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
