import nba_monitor as nm
from datetime import datetime, timezone

import resumen_papel
from papel import ABIERTA, COMPRA_PENDIENTE, ESTRATEGIAS, Filtros, SimuladorPapel


def fila(bids, asks, volumen=50_000, token="1", inicio=None):
    mercado = nm.Mercado("A vs B", "Ganador", "A", token, inicio, volumen)
    return nm.analizar(mercado, nm.Libro(bids=bids, asks=asks))


def test_vuelta_completa_por_operaciones():
    sim = SimuladorPapel(presupuesto=7, acciones=5)
    f = fila({0.47: 1000}, {0.50: 1000})
    sim.paso({"1": f}, [], ahora=0)
    pos = sim.posiciones["1"]
    assert (pos.estado, pos.precio_compra, pos.precio_venta, pos.stop) == (COMPRA_PENDIENTE, 0.48, 0.49, 0.45)
    # alguien vende a 0.47: habría pegado primero contra tu compra a 0.48
    sim.paso({"1": f}, [{"asset_id": "1", "price": "0.47", "side": "SELL"}], ahora=1)
    assert sim.posiciones["1"].estado == ABIERTA
    sim.paso({"1": f}, [{"asset_id": "1", "price": "0.50", "side": "BUY"}], ahora=5)
    [c] = sim.cerradas
    assert (c.motivo, round(c.ganancia, 4), c.segundos) == ("objetivo", 0.05, 4)
    # vuelve a poner una compra porque sigue habiendo oportunidad
    assert sim.posiciones["1"].estado == COMPRA_PENDIENTE


def test_stop_con_comision():
    sim = SimuladorPapel(acciones=5, stop=0.03, comision_taker=0.01)
    sim.paso({"1": fila({0.47: 1000}, {0.50: 1000})}, [], ahora=0)
    sim.paso({"1": fila({0.47: 1000}, {0.48: 1000})}, [], ahora=1)  # aparece venta a tu precio: llena
    assert sim.posiciones["1"].estado == ABIERTA
    sim.paso({"1": fila({0.44: 1000}, {0.48: 1000})}, [], ahora=2)
    [c] = sim.cerradas
    assert c.motivo == "stop"
    assert round(c.ganancia, 4) == round((0.44 - 0.48) * 5 - 0.44 * 5 * 0.01, 4)


def test_cancela_si_lo_mejoran_o_pasa_el_tiempo():
    sim = SimuladorPapel(espera=60)
    sim.paso({"1": fila({0.47: 1000}, {0.50: 1000})}, [], ahora=0)
    sim.paso({"1": fila({0.49: 1000}, {0.50: 1000})}, [], ahora=1)
    assert sim.canceladas == 1 and "1" not in sim.posiciones
    sim.paso({"1": fila({0.47: 1000}, {0.50: 1000})}, [], ahora=2)
    sim.paso({"1": fila({0.47: 1000}, {0.50: 1000})}, [], ahora=100)
    assert sim.canceladas == 2


def test_respeta_presupuesto():
    sim = SimuladorPapel(presupuesto=3, acciones=5)
    filas = {t: fila({0.47: 1000}, {0.50: 1000}, token=t) for t in ("1", "2")}
    sim.paso(filas, [], ahora=0)
    assert len(sim.posiciones) == 1  # 5 x 0.48 = 2.40; una segunda pasaría de $3


def test_filtro_pre_partido():
    inicio = datetime.fromtimestamp(100, timezone.utc)
    sim = SimuladorPapel("B", Filtros(solo_pre_partido=True))
    sim.paso({"1": fila({0.47: 1000}, {0.50: 1000}, inicio=inicio)}, [], ahora=50)
    assert "1" in sim.posiciones
    # empieza el partido: se cancela la compra pendiente y no se vuelve a entrar
    sim.paso({"1": fila({0.47: 1000}, {0.50: 1000}, inicio=inicio)}, [], ahora=101)
    assert sim.canceladas == 1 and "1" not in sim.posiciones
    sim.paso({"1": fila({0.47: 1000}, {0.50: 1000}, inicio=inicio)}, [], ahora=102)
    assert "1" not in sim.posiciones


def test_filtro_rango_de_precio():
    sim = SimuladorPapel("B", Filtros(precio_min=0.20, precio_max=0.80))
    sim.paso({"1": fila({0.13: 1000}, {0.16: 1000})}, [], ahora=0)
    assert not sim.posiciones
    sim.paso({"1": fila({0.47: 1000}, {0.50: 1000})}, [], ahora=1)
    assert "1" in sim.posiciones


def test_filtro_caida_reciente():
    sim = SimuladorPapel("B", Filtros(caida=0.01, ventana=30), espera=1)
    sim.paso({"1": fila({0.50: 1000}, {0.53: 1000})}, [], ahora=0)  # entra: no hay historial
    sim.posiciones.clear()
    sim.paso({"1": fila({0.48: 1000}, {0.51: 1000})}, [], ahora=5)  # bajó 2¢ en 5 s: no entra
    assert not sim.posiciones
    sim.paso({"1": fila({0.48: 1000}, {0.51: 1000})}, [], ahora=40)  # la caída ya salió de la ventana
    assert "1" in sim.posiciones


def test_dos_estrategias_con_los_mismos_datos(tmp_path):
    ruta = tmp_path / "papel.csv"
    a = SimuladorPapel("A", ESTRATEGIAS["A"], csv_ruta=ruta)
    b = SimuladorPapel("B", ESTRATEGIAS["B"], csv_ruta=ruta)
    barato = fila({0.13: 1000}, {0.16: 1000})  # B lo descarta por precio
    for s in (a, b):
        s.paso({"1": barato}, [], ahora=0)
        s.paso({"1": barato}, [{"asset_id": "1", "price": "0.13", "side": "SELL"}], ahora=1)
        s.paso({"1": barato}, [{"asset_id": "1", "price": "0.16", "side": "BUY"}], ahora=2)
    assert len(a.cerradas) == 1 and not b.cerradas
    filas = resumen_papel.leer([ruta])
    assert [f["estrategia"] for f in filas] == ["A"]
    grupos = resumen_papel.agrupar(filas)
    assert grupos[("Total", "A")]["objetivos"] == 1


def test_resumen_lee_csv_viejo_sin_estrategia(tmp_path):
    viejo = tmp_path / "viejo.csv"
    viejo.write_text("partido,tipo,resultado,precio_compra,precio_salida,acciones,motivo,ganancia,segundos,cerrada\n"
                     "X,O/U,Over,0.63,0.58,5,stop,-0.25,0.6,2026-10-23 18:22:10\n"
                     "X,O/U,Over,0.40,0.42,5,objetivo,0.1,110.8,2026-10-24 19:08:44\n", encoding="utf-8")
    grupos = resumen_papel.agrupar(resumen_papel.leer([viejo]))
    assert grupos[("2026-10-23", "A")]["stops"] == 1
    assert round(grupos[("Total", "A")]["ganancia"], 2) == -0.15
