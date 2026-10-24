import nba_monitor as nm
from papel import ABIERTA, COMPRA_PENDIENTE, SimuladorPapel


def fila(bids, asks, volumen=50_000, token="1"):
    mercado = nm.Mercado("A vs B", "Ganador", "A", token, None, volumen)
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
