import json

import marcador as mc


def test_combina_mensajes_y_resume():
    m = mc.Marcador()
    m.procesar(json.dumps({"slug": "nba-lal-uta-2026-10-25", "live": True, "score": "83-77",
                           "period": "Q3", "elapsed": "04:12"}))
    m.procesar(json.dumps([{"slug": "atp-x-y", "score": "6-4"},
                           {"slug": "nba-lal-uta-2026-10-25", "score": "85-77"}]))  # solo lo que cambió
    r = m.de("nba-lal-uta-2026-10-25")
    assert r == {"equipos": [{"siglas": "LAL", "puntos": 85}, {"siglas": "UTA", "puntos": 77}],
                 "cuarto": "Q3", "reloj": "04:12", "estado": "en juego"}
    assert m.de("atp-x-y") is None and m.de("nba-mia-nyk-2026-10-25") is None


def test_final_y_por_empezar():
    assert mc.resumir("nba-ind-bkn-2026-10-25", {"ended": True, "live": False, "score": "112-104"})["estado"] == "final"
    sin_empezar = mc.resumir("nba-ind-bkn-2026-10-25", {"live": False})
    assert sin_empezar["estado"] == "por empezar"
    assert [e["puntos"] for e in sin_empezar["equipos"]] == [None, None]


def test_ignora_basura():
    m = mc.Marcador()
    m.procesar("ping")
    m.procesar("{no es json")
    m.procesar(json.dumps([1, "x", {"score": "1-0"}]))
    assert m.partidos == {}
