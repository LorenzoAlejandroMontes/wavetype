"""
Banco di context.py: la regola "continua la frase" (maiuscola e spazio giusti).

Solo regola pura, niente UIA: quello si prova a schermo con tests/bench/context/.

Uso:
  .venv/Scripts/python.exe tests/test_context.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import context as C                                          # noqa: E402

VOCAB = ["NTC", "CV Maker", "Wavetype", "Mobbin"]


def eq(got, want, what=""):
    assert got == want, f"{what}: {got!r} != {want!r}"


def test_non_si_sa_resta_come_prima():
    eq(C.starts_sentence(None), True, "None")
    eq(C.fit(None, "Ciao a tutti."), "Ciao a tutti.")


def test_inizio_frase():
    for prev in ["", "   ", "Ho finito il lavoro. ", "Ho finito!", "Davvero?", "Ecco:",
                 "prima riga\n", "prima riga\n   ", "Va bene…", "punto e virgola;"]:
        eq(C.starts_sentence(prev), True, repr(prev))


def test_meta_frase():
    for prev in ["Ciao a tutti, questa e' una fr", "quindi volevo dirti che", "e poi",
                 "il totale e' 42", "una lista di cose,"]:
        eq(C.starts_sentence(prev), False, repr(prev))


def test_chiusure_si_guarda_dentro():
    eq(C.starts_sentence("(come detto)"), False)       # parentesi chiusa dopo una parola
    eq(C.starts_sentence('disse "vengo."'), True)      # il punto sta dentro le virgolette
    eq(C.starts_sentence("(finito.)"), True)


def test_abbassa_e_spazia():
    eq(C.fit("quindi volevo dirti che", "Domani arrivo presto."),
       " domani arrivo presto.")


def test_niente_spazio_doppio():
    eq(C.fit("quindi volevo dirti che ", "Domani arrivo."), "domani arrivo.")
    eq(C.fit("apro la parentesi (", "Domani arrivo."), "domani arrivo.")


def test_nomi_e_sigle_restano_maiuscoli():
    eq(C.fit("ne ho parlato con", "Mobbin e' sempre accessibile.", VOCAB),
       " Mobbin e' sempre accessibile.")
    eq(C.fit("il documento sul", "GDPR va riletto."), " GDPR va riletto.")
    eq(C.fit("and then", "I will call you."), " I will call you.")


def test_inizio_frase_non_tocca_niente():
    eq(C.fit("Ho finito. ", "Domani arrivo presto."), "Domani arrivo presto.")
    eq(C.fit("riga finita\n", "Domani arrivo."), "Domani arrivo.")


def test_lista_e_testo_multiriga():
    # una lista dettata dentro una frase aperta: si abbassa solo la prima lettera
    eq(C.fit("mi servono", "Pane, latte e uova."), " pane, latte e uova.")
    eq(C.fit("ecco la lista:", "Pane\n- latte"), "Pane\n- latte")


def test_testo_vuoto_o_spazi():
    eq(C.fit("quindi", ""), "")
    eq(C.fit("quindi", "   "), "   ")


def main():
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
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
