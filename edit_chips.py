"""
edit_chips.py — i tre comandi di Edit Mode: definizioni, testi dell'istruzione, riconoscimento.

La card di Edit e' una command bar con tre chip numerati,
    1 Grammar · 2 English · 3 Slack
Si sceglie in due modi: premendo 1, 2 o 3 (niente voce, zero errori di trascrizione) oppure
dicendo il comando. Qualunque altra frase resta un'istruzione libera e va al modello com'e'.

Qui dentro non si disegna niente e non si chiama nessuna rete: solo dati e una funzione di
confronto. La card (live_panel.py) e l'orchestratore (wavetype.py) importano questo modulo.

Tre cose:
    CHIPS            (Chip, Chip, Chip) in ordine: n = 1, 2, 3
    by_key("2")      il chip di un tasto, o None
    match("...")     l'id del chip se la frase detta E' quel comando, altrimenti None

Regola di `match`: **conservativa**. Il chip si accende solo se la frase
e' in pratica solo quel comando ("correggi la grammatica", "in inglese", "tono slack"), con al
massimo una cortesia davanti ("me lo puoi fare...") e un complemento oggetto dietro
("...di questa frase"). Appena c'e' contenuto in piu' ("traduci in inglese e accorcialo")
resta istruzione libera: meglio non accendere un chip che accenderne uno sbagliato.
Misurato su 43 istruzioni Edit reali (agosto-settembre 2026): 4 -> Grammar,
2 -> English, 0 -> Slack, 37 libere, 0 falsi positivi (controllate una per una).

`instr` e' il testo che l'orchestratore passa come argomento `instr` di EDIT_PROMPT
(wavetype.py:735). Il prompt contiene gia' le regole di traduzione: per English basta una riga.
"""
import re
import unicodedata
from collections import namedtuple

# n = numero del chip e tasto; id = chiave interna; label = etichetta sulla card (inglese, mercato
# US); short = etichetta quando la card e' stretta; done = pillola di esito; instr = istruzione
# per il modello (in italiano come gli altri prompt).
Chip = namedtuple("Chip", "n key id label short done instr")

GRAMMAR_INSTR = (
    "Correggi gli errori di grammatica, ortografia, punteggiatura e sintassi, e rendi il "
    "messaggio piu' naturale e scorrevole da leggere. Mantieni la lingua del testo, il "
    "significato, il tono, i nomi propri e tutte le informazioni presenti: non aggiungere "
    "niente e non togliere niente. Non trasformare il testo in un elenco puntato: se e' un "
    "discorso continuo resta un discorso continuo."
)

ENGLISH_INSTR = "Traduci il testo in inglese."

SLACK_INSTR = (
    "Riscrivi il testo come un messaggio di chat di lavoro su Slack: tono naturale da collega, "
    "diretto, paragrafi brevi. Mantieni la lingua del testo, il significato e tutte le "
    "informazioni presenti. Non inventare saluti, aperture o firme che non ci sono, non "
    "aggiungere emoji, non trasformare il testo in un elenco puntato se non lo era."
)

CHIPS = (
    Chip(1, "1", "grammar", "Grammar", "1", "GRAMMAR FIXED", GRAMMAR_INSTR),
    Chip(2, "2", "english", "English", "2", "NOW IN ENGLISH", ENGLISH_INSTR),
    Chip(3, "3", "slack", "Slack", "3", "READY FOR SLACK", SLACK_INSTR),
)

FREE_DONE = "REWRITTEN"          # esito di un'istruzione libera: stessa forma delle tre pillole

_BY_ID = {c.id: c for c in CHIPS}
_BY_KEY = {c.key: c for c in CHIPS}
_BY_N = {c.n: c for c in CHIPS}


def by_key(key):
    """Il chip del tasto premuto ("1", "2", "3", oppure 1, 2, 3), o None."""
    if isinstance(key, int):
        return _BY_N.get(key)
    return _BY_KEY.get((key or "").strip())


def by_id(cid):
    return _BY_ID.get(cid)


def done_text(cid):
    """La pillola di esito: quella del chip, o REWRITTEN per un'istruzione libera."""
    c = _BY_ID.get(cid)
    return c.done if c else FREE_DONE


def instruction(cid):
    """Il testo da passare come `instr` a EDIT_PROMPT. None per un'istruzione libera."""
    c = _BY_ID.get(cid)
    return c.instr if c else None


# ------------------------------------------------------------------ riconoscimento
_PUNCT = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACES = re.compile(r"\s+")
MAX_WORDS = 6                    # oltre tanto non e' piu' "solo il comando": mai un chip


def norm(text):
    """Forma di confronto: senza accenti, senza punteggiatura, minuscola, spazi normali.
    Gli accenti vanno via perche' il dettato li sbaglia ("gramatica", "ingles")."""
    t = unicodedata.normalize("NFD", (text or "").lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return _SPACES.sub(" ", _PUNCT.sub(" ", t)).strip()


# cortesie e giri di frase che possono stare DAVANTI al comando senza cambiarlo
_PREFIX = (
    "ok", "okay", "allora", "senti", "ecco", "dai", "per favore", "perfavore", "ti prego",
    "puoi", "me lo puoi fare", "melo puoi fare", "mi puoi fare", "puoi farmi", "puoi farlo",
    "potresti", "potresti farlo", "mi fai", "fammi", "fammelo", "please", "can you",
    "could you", "por favor", "puedes",
)

# complementi che possono stare DIETRO al comando senza aggiungere contenuto
_SUFFIX = (
    "per favore", "perfavore", "grazie", "please", "gracias", "por favor",
    "questo", "questa", "questo testo", "questa frase", "questo messaggio", "il testo",
    "la frase", "il messaggio", "di questo", "di questa", "di questo testo",
    "di questa frase", "di questo messaggio", "del testo", "della frase", "qui", "qua",
    "qui sopra", "this", "it", "this text", "the text", "esto", "este texto",
)


def _strip_list(core, items, leading):
    """Toglie UNA volta il pezzo piu' lungo della lista, davanti o dietro."""
    for piece in sorted(items, key=len, reverse=True):
        if leading and core.startswith(piece + " "):
            return core[len(piece) + 1:].strip()
        if not leading and core.endswith(" " + piece):
            return core[:-(len(piece) + 1)].strip()
    return core


def _cross(verbs, objects):
    """Tutte le forme "verbo oggetto" piu' gli oggetti nudi ("grammatica", "in inglese")."""
    out = set(objects)
    for v in verbs:
        for o in objects:
            out.add(f"{v} {o}")
    return out


# --- 1 · Grammar (grammatica E sintassi, e il messaggio suona meglio)
_G_VERBS = ("correggi", "corregi", "correggimi", "sistema", "sistemami", "aggiusta",
            "aggiustami", "controlla", "rivedi", "ricontrolla",
            "fix", "fix the", "check", "check the", "correct", "correct the",
            "corrige", "corrigeme", "arregla", "revisa")
_G_OBJ = ("la grammatica", "grammatica", "la gramatica", "gramatica",
          "la grammatica e la sintassi", "grammatica e sintassi",
          "la grammatica e la punteggiatura", "grammatica e punteggiatura",
          "la sintassi", "sintassi", "gli errori", "gli errori di grammatica",
          "errori di grammatica", "la forma",
          "grammar", "the grammar", "grammar and syntax", "spelling", "the spelling",
          "spelling and grammar", "grammar and spelling",
          "la gramatica y la sintaxis", "gramatica y sintaxis", "la ortografia")
_G_CLITIC = ("sistemala", "sistemalo", "sistemali", "correggila", "correggilo", "corregila",
             "corregilo", "aggiustala", "aggiustalo", "riscrivila", "riscrivilo")
_G_ADV = ("grammaticalmente", "dal punto di vista grammaticale")

_GRAMMAR = _cross(_G_VERBS, _G_OBJ)
_GRAMMAR |= {f"{v} {a}" for v in _G_VERBS + _G_CLITIC for a in _G_ADV}
_GRAMMAR |= set(_G_ADV)
_GRAMMAR |= {"proofread", "proof read", "grammatica e basta", "solo la grammatica",
             "solo grammatica", "correggi", "correggila", "correggilo"}

# --- 2 · English
_E_VERBS = ("traduci", "traducilo", "traducila", "traducimelo", "traducimela", "mettilo",
            "mettila", "metti", "fallo", "falla", "scrivilo", "scrivila", "riscrivilo",
            "riscrivila", "girala", "giralo", "dillo", "voglio",
            "translate", "translate it", "put it", "make it", "write it", "rewrite it", "say it",
            "traduce", "traducelo", "traducela", "ponlo", "pasalo", "escribelo")
_E_OBJ = ("in inglese", "in english", "to english", "into english", "in inglés",
          "en ingles", "en inglesa", "al ingles", "inglese", "english", "ingles", "inglesa",
          "in inglese naturale", "in english please")
_ENGLISH = _cross(_E_VERBS, _E_OBJ)

# --- 3 · Slack
_S_VERBS = ("mettilo", "mettila", "metti", "fallo", "falla", "scrivilo", "scrivila",
            "riscrivilo", "riscrivila", "adattalo", "adattala", "buttalo",
            "make it", "write it", "rewrite it", "put it")
_S_OBJ = ("tono slack", "in tono slack", "tono da slack", "con tono slack", "per slack",
          "da slack", "in slack", "stile slack", "in stile slack", "come su slack",
          "come un messaggio slack", "messaggio slack", "slack",
          "slack tone", "in slack tone", "slack style", "for slack", "like slack",
          "tono de slack", "para slack", "estilo slack")
_SLACK = _cross(_S_VERBS, _S_OBJ)

_TABLE = [("grammar", _GRAMMAR), ("english", _ENGLISH), ("slack", _SLACK)]


def core_of(text):
    """Il cuore della frase: normalizzata, senza cortesia davanti e complemento dietro.
    Esposta perche' i test la guardano; l'app usa solo match()."""
    core = norm(text)
    for _ in range(2):                       # "ok, per favore, ..." = due giri bastano
        before = core
        core = _strip_list(core, _PREFIX, True)
        core = _strip_list(core, _SUFFIX, False)
        if core == before:
            break
    return core


def match(text):
    """L'id del chip ("grammar" | "english" | "slack") se la frase E' quel comando, se no None.
    Non solleva mai: qualunque cosa arrivi dal riconoscitore, al massimo torna None."""
    try:
        core = core_of(text)
    except Exception:
        return None
    if not core or len(core.split()) > MAX_WORDS:
        return None
    for cid, phrases in _TABLE:
        if core in phrases:
            return cid
    return None
