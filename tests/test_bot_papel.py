from datetime import datetime, timezone

import cuotas as cu
import nba_monitor as nm
from bot_papel import BotComparador, BotSpread, SimuladorPausas, leer_cerradas
from papel import COMPRA_PENDIENTE
from test_cuotas import EVENTO_ODDS, EVENTO_PM, INICIO, libro


def fila(bids, asks, inicio):
    return nm.analizar(nm.Mercado("A vs B", "Ganador", "A", "1", inicio, 50_000), nm.Libro(bids=bids, asks=asks))


def test_spread_guarda_y_recupera_el_saldo(tmp_path):
    ruta = tmp_path / "bot_spread.csv"
    bot = BotSpread(7, csv_ruta=ruta)
    antes = datetime.fromtimestamp(10_000, timezone.utc)  # el partido empieza después: B puede entrar
    bot.paso({"1": fila({0.47: 1000}, {0.50: 1000}, antes)}, [], ahora=0)
    bot.paso({"1": fila({0.47: 1000}, {0.48: 1000}, antes)}, [], ahora=1)  # se llena la compra a 0.48
    bot.paso({"1": fila({0.44: 1000}, {0.48: 1000}, antes)}, [], ahora=2)  # stop
    foto = bot.foto()
    assert round(foto["realizado"], 2) == -0.20 and round(foto["saldo"], 2) == 6.80
    # al volver a abrir el dashboard el saldo sigue donde se quedó
    assert [c.motivo for c in leer_cerradas(ruta)] == ["stop"]
    assert round(BotSpread(7, csv_ruta=ruta).foto()["saldo"], 2) == 6.80


def test_spread_no_usa_lo_perdido():
    bot = BotSpread(7, csv_ruta=None)
    bot.sim.cerradas = [cu_cerrada(-6.0)]
    bot.paso({}, [])
    assert bot.sim.presupuesto == 1.0


def cu_cerrada(ganancia):
    from papel import Cerrada
    return Cerrada("B", "A vs B", "Ganador", "A", 0.5, 0.4, 5, "stop", ganancia, 1, 0)


def comparador(tmp_path, **kw):
    bot = BotComparador("llave", csv_ruta=tmp_path / "bot_comparador.csv", **kw)
    bot.mercados = [m for m in cu.mercados_pm_de_evento(EVENTO_PM) if m.tipo == "h2h"]
    bot.eventos = [EVENTO_ODDS]
    return bot


def test_comparador_compra_y_vende_al_precio_justo(tmp_path):
    bot = comparador(tmp_path, min_ventaja=0.03)
    antes = INICIO.replace(hour=20)
    bot.libros = {"t1": libro(0.50, 0.52)}  # Lakers a 0.52, justo 0.602
    bot.evaluar(antes)
    [a] = bot.apuestas
    assert (a.estrategia, a.lado, a.precio, a.venta) == ("vender_justo", "Lakers", 0.52, "0.60")
    assert round(bot.libre(), 2) == 6.00
    bot.evaluar(antes)  # no vuelve a comprar el mismo lado
    assert len(bot.apuestas) == 1
    bot.libros = {"t1": libro(0.60, 0.62)}  # alguien ya paga 0.60
    bot.evaluar(antes)
    assert a.resultado == "vendida"
    foto = bot.foto()
    assert round(foto["realizado"], 4) == round(1 / 0.52 * 0.60 - 1, 4)
    assert foto["cerradas"][0]["motivo"] == "vendida" and foto["abiertas"] == []
    # se guardó: al reabrir sigue ahí
    assert len(BotComparador("llave", csv_ruta=bot.csv_ruta).apuestas) == 1


def test_comparador_respeta_el_saldo(tmp_path):
    bot = comparador(tmp_path, presupuesto=0.5, min_ventaja=0.03)
    bot.libros = {"t1": libro(0.50, 0.52)}
    bot.evaluar(INICIO.replace(hour=20))
    assert bot.apuestas == []


def test_comparador_sin_llave_o_en_demo(tmp_path):
    assert "llave" in BotComparador(None, csv_ruta=tmp_path / "x.csv").foto()["mensaje"]
    assert "demo" in BotComparador("llave", csv_ruta=tmp_path / "x.csv", demo=True).foto()["mensaje"]


def test_pausas_solo_compra_en_pausa_y_cancela_al_reanudarse():
    pausa = {"valor": False}
    sim = SimuladorPausas(lambda token: pausa["valor"], 7, None)
    durante = datetime.fromtimestamp(0, timezone.utc)  # el partido ya empezó
    f = fila({0.47: 1000}, {0.50: 1000}, durante)
    sim.paso({"1": f}, [], ahora=10)
    assert sim.posiciones == {}                   # reloj corriendo: no entra
    pausa["valor"] = True
    sim.paso({"1": f}, [], ahora=11)
    assert sim.posiciones["1"].estado == COMPRA_PENDIENTE
    pausa["valor"] = False                        # se reanuda antes de llenarse
    sim.paso({"1": f}, [], ahora=12)
    assert sim.posiciones == {} and sim.canceladas == 1


def test_pausas_conserva_lo_ya_comprado():
    pausa = {"valor": True}
    sim = SimuladorPausas(lambda token: pausa["valor"], 7, None)
    durante = datetime.fromtimestamp(0, timezone.utc)
    sim.paso({"1": fila({0.47: 1000}, {0.50: 1000}, durante)}, [], ahora=10)
    sim.paso({"1": fila({0.47: 1000}, {0.48: 1000}, durante)}, [], ahora=11)  # se llena a 0.48
    pausa["valor"] = False
    sim.paso({"1": fila({0.47: 1000}, {0.48: 1000}, durante)}, [], ahora=12)
    assert sim.posiciones["1"].estado != COMPRA_PENDIENTE  # sigue esperando su venta
