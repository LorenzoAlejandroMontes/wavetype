"""
Banco di derail_score: riconoscere quando Whisper "deraglia" su un audio lungo (smette di mettere
punteggiatura e insieme lascia cadere accenti e parole corte).

L'impronta viene da riprove sullo stesso wav di 435 s (22/09/2026): una richiesta sola deraglia con
corse di 69 e 157 parole, lo stesso audio a pezzi da 60 s torna pulito. I campioni qui sotto sono
scritti apposta e riproducono quella forma; il testo dettato vero non entra in una repo pubblica.

Uso:
  .venv/Scripts/python.exe tests/test_derail.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import wavetype as S                                         # noqa: E402

# la forma del deragliamento: una corsa lunga senza punteggiatura, e dentro le parole italiane
# rimaste tronche ("attivit", "perch", "priorit", "pi", "pu")
DERAGLIATA = (
    "allora la prima cosa da fare domani mattina sarebbe rivedere insieme la lista delle attivit "
    "che abbiamo aperto perch secondo me non tutte hanno la stessa priorit e qualcuna la possiamo "
    "spostare alla settimana prossima senza problemi poi volevo capire se la parte di ricerca la "
    "portiamo avanti noi oppure se pi sensato chiedere una mano fuori visto che da soli non ce la "
    "facciamo e questo pu diventare un problema serio quando arriviamo alla scadenza di fine mese."
)

# lo stesso parlato trascritto bene: accenti interi e punteggiatura al suo posto
SANA = (
    "Allora, la prima cosa da fare domani mattina sarebbe rivedere insieme la lista delle "
    "attivita' che abbiamo aperto. Secondo me non tutte hanno la stessa priorita', e qualcuna la "
    "possiamo spostare alla settimana prossima senza problemi. Poi volevo capire se la parte di "
    "ricerca la portiamo avanti noi, oppure se e' piu' sensato chiedere una mano fuori: da soli "
    "non ce la facciamo, e questo puo' diventare un problema quando arriviamo a fine mese."
)


def eq(got, want, what=""):
    assert got == want, f"{what}: {got!r} != {want!r}"


def test_riconosce_il_deragliamento():
    n = S.derail_score(DERAGLIATA)
    assert n >= S.DERAIL_MIN_RUN, f"non riconosciuta: {n}"


def test_non_scatta_sul_testo_sano():
    eq(S.derail_score(SANA), 0, "testo sano")


def test_non_scatta_su_una_dettatura_corta_senza_punteggiatura():
    # corta e senza punti: puo' capitare e non e' un deragliamento, il testo e' tutto li'
    eq(S.derail_score("ciao come stai oggi tutto bene spero di si fammi sapere piu' tardi"), 0,
       "dettatura corta")


def test_serve_la_coppia_corsa_lunga_piu_parola_tronca():
    # 80 parole senza punteggiatura ma con gli accenti al loro posto: non e' un deragliamento
    sano = " ".join(["perche' oggi va tutto bene e non c'e' niente da dire"] * 8)
    eq(S.derail_score(sano), 0, "corsa lunga ma accenti interi")
    # le stesse parole con gli accenti caduti: deragliata
    assert S.derail_score(sano.replace("perche'", "perch")) > 0, "corsa lunga con parole tronche"


def test_una_frase_punteggiata_spezza_la_corsa():
    pezzo = " ".join(["perch oggi va tutto bene e non c'e' niente da dire."] * 8)
    eq(S.derail_score(pezzo), 0, "punteggiatura ogni 10 parole")


def main():
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    bad = 0
    for n, f in tests:
        try:
            f()
            print(f"ok   {n}")
        except Exception as ex:
            bad += 1
            print(f"FAIL {n}: {ex!r}")
    print(f"{len(tests) - bad}/{len(tests)} ok" if not bad else f"{bad} falliti")
    return bad


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
