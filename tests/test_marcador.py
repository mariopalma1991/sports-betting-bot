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
                 "cuarto": "Q3", "reloj": "04:12", "estado": "en juego", "pausa": False}
    assert m.de("nba-mia-nyk-2026-10-25") is None


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


def test_pausa_cuando_el_reloj_no_avanza():
    m = mc.Marcador()
    slug = "nba-lal-uta-2026-10-25"
    m.procesar(json.dumps({"slug": slug, "live": True, "score": "83-77", "elapsed": "04:12"}), ahora=0)
    m.procesar(json.dumps({"slug": slug, "elapsed": "04:10"}), ahora=2)
    assert not m.en_pausa(slug, ahora=4)          # el reloj cambió hace 2 s
    assert m.en_pausa(slug, ahora=2 + mc.PAUSA_RELOJ)  # tiempo fuera: el reloj no se mueve
    # tiro libre: el reloj sigue detenido pero alguien anotó
    m.procesar(json.dumps({"slug": slug, "score": "84-77"}), ahora=10)
    assert not m.en_pausa(slug, ahora=11)
    assert m.en_pausa(slug, ahora=10 + mc.PAUSA_PUNTOS)
    # mismo reloj repetido no cuenta como cambio
    m.procesar(json.dumps({"slug": slug, "elapsed": "04:10"}), ahora=20)
    assert m.en_pausa(slug, ahora=20)


def test_sin_pausa_si_no_esta_en_juego():
    m = mc.Marcador()
    m.procesar(json.dumps({"slug": "nba-a-b-2026-10-25", "live": False, "ended": True, "elapsed": "00:00"}), ahora=0)
    assert not m.en_pausa("nba-a-b-2026-10-25", ahora=100)
    assert not m.en_pausa("nba-x-y-2026-10-25", ahora=100)


def test_slug_del_canal_sin_nba():
    # El canal de deportes manda "ind-bkn-…"; el evento de Polymarket es "nba-ind-bkn-…".
    m = mc.Marcador()
    m.procesar(json.dumps({"slug": "ind-bkn-2026-10-25", "live": True, "score": "102-84",
                           "period": "Q4", "elapsed": "03:21"}), ahora=0)
    r = m.de("nba-ind-bkn-2026-10-25")
    assert r["reloj"] == "03:21" and r["cuarto"] == "Q4"
    assert [e["puntos"] for e in r["equipos"]] == [102, 84]
    assert m.en_pausa("nba-ind-bkn-2026-10-25", ahora=10)


def test_busca_por_equipos_cuando_no_hay_slug():
    # Así llega del canal real: sin slug, con nombres completos y "local-visitante".
    m = mc.Marcador()
    m.titulos = {"nba-tor-min-2026-10-25": "Raptors vs. Timberwolves"}
    m.procesar(json.dumps([
        {"gameId": 1, "leagueAbbreviation": "fpd", "homeTeam": "LD Alajuelense", "awayTeam": "X",
         "score": "0-0", "live": True},
        {"gameId": 2, "leagueAbbreviation": "nba", "homeTeam": "Minnesota Timberwolves",
         "awayTeam": "Toronto Raptors", "status": "InProgress", "score": "55-47",
         "period": "Q2", "elapsed": "03:10", "live": True, "ended": False},
    ]), ahora=0)
    r = m.de("nba-tor-min-2026-10-25")
    assert r["equipos"] == [{"siglas": "TOR", "puntos": 47}, {"siglas": "MIN", "puntos": 55}]
    assert r["cuarto"] == "Q2" and r["reloj"] == "03:10" and r["estado"] == "en juego"
    m.procesar(json.dumps({"gameId": 2, "elapsed": "03:05"}), ahora=1)  # solo lo que cambió
    assert m.de("nba-tor-min-2026-10-25")["reloj"] == "03:05"
    assert m.de("nba-lal-uta-2026-10-25") is None


def test_busca_por_siglas():
    # La NFL llega con siglas ("SEA", "KC"); la NBA podría llegar igual.
    m = mc.Marcador()
    m.titulos = {"nba-lal-uta-2026-10-25": "Lakers vs. Jazz"}
    m.procesar(json.dumps({"gameId": 3, "leagueAbbreviation": "NBA", "homeTeam": "UTA", "awayTeam": "LAL",
                           "score": "77-83", "period": "Q3", "elapsed": "04:12", "live": True}))
    r = m.de("nba-lal-uta-2026-10-25")
    assert [(e["siglas"], e["puntos"]) for e in r["equipos"]] == [("LAL", 83), ("UTA", 77)]
