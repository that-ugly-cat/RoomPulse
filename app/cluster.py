"""Clustering a due assi delle risposte argpoll (claim + justification) via Claude.

Pattern AutoCode: una chiamata, output JSON strutturato. Due raggruppamenti DESCRITTIVI:
- claim_clusters: i criteri proposti, raggruppati per sostanza.
- arg_clusters:   le giustificazioni, raggruppate per IL TIPO DI CONSIDERAZIONE a cui
                  fanno appello (tematico, mai valutativo: niente "fallace/circolare/debole").
Poi assegna ogni risposta a (claim_cluster, arg_cluster). La matrice claim×arg ne deriva.

La chiave API è per-utente (passata a runtime). Modello: Sonnet di default.
"""

import json

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4000

SYSTEM = """Sei un analista di argomentazione. Ricevi una lista numerata di risposte, ognuna con un CLAIM (un criterio proposto) e una JUSTIFICATION (la ragione data a supporto).

Produci DUE raggruppamenti distinti, entrambi DESCRITTIVI e mai valutativi:

1. claim_clusters — raggruppa i CLAIM per criterio sostanziale. L'etichetta è il criterio in forma breve e neutra (es. "Massimizzare il bene aggregato", "Proteggere i vulnerabili").

2. arg_clusters — raggruppa le JUSTIFICATION per IL TIPO DI CONSIDERAZIONE A CUI FANNO APPELLO, NON per il claim che sostengono. L'etichetta è tematica e descrittiva, in stile "Appello a…": es. "Appello all'imparzialità", "Appello alle conseguenze", "Appello alla protezione dei deboli", "Appello al contributo sociale". Descrivono a cosa fa appello l'argomento, senza giudicarne la validità. Non usare MAI termini valutativi come "fallace", "circolare", "debole", "non etico", "valido". Punta a 4–8 gruppi.

Lo stesso criterio può essere sostenuto da appelli diversi, e appelli uguali possono sostenere criteri diversi: è normale e voluto.

Poi assegna OGNI risposta (per numero) a esattamente un claim_cluster e un arg_cluster.

Usa la stessa lingua delle risposte per le etichette. Restituisci SOLO JSON valido, senza testo prima o dopo, in questo formato esatto:
{
  "claim_clusters": [{"id": "c1", "label": "..."}],
  "arg_clusters": [{"id": "a1", "label": "..."}],
  "assignments": [{"n": 1, "claim": "c1", "arg": "a1"}]
}"""


SYSTEM_OPENTEXT = """Raggruppi le risposte a testo libero a una domanda in cluster TEMATICI, descrittivi e mai valutativi.

Ricevi la DOMANDA posta e una lista numerata di RISPOSTE. Produci:

- clusters — gruppi tematici delle risposte. L'etichetta descrive il tema del gruppo in forma breve e neutra, nella lingua delle risposte, alla luce della domanda. È DESCRITTIVA (nomina di cosa parla il gruppo), mai valutativa: non usare MAI termini come "fallace", "circolare", "debole", "non etico", "giusto", "sbagliato". Punta a 4–8 gruppi.

- assignments — assegna OGNI risposta (per numero) a esattamente un cluster.

Restituisci SOLO JSON valido, senza testo prima o dopo, in questo formato esatto:
{
  "clusters": [{"id": "t1", "label": "..."}],
  "assignments": [{"n": 1, "cluster": "t1"}]
}"""


def _build_user_msg(question: str, pairs: list) -> str:
    lines = [f"DOMANDA: {question}", "", "Risposte:"]
    for p in pairs:
        lines.append(f'{p["n"]}. CLAIM: {p["claim"]} | JUSTIFICATION: {p["justification"]}')
    return "\n".join(lines)


def _build_opentext_msg(question: str, texts: list) -> str:
    lines = [f"DOMANDA: {question}", "", "Risposte:"]
    for t in texts:
        lines.append(f'{t["n"]}. {t["text"]}')
    return "\n".join(lines)


def _parse(text: str) -> dict:
    t = text.strip()
    if t.startswith("```"):  # togli eventuali fence markdown
        t = t.split("```", 2)[1]
        if t.lstrip().startswith("json"):
            t = t.lstrip()[4:]
    # ritaglia dal primo { all'ultimo }
    a, b = t.find("{"), t.rfind("}")
    if a != -1 and b != -1:
        t = t[a : b + 1]
    data = json.loads(t)
    data.setdefault("claim_clusters", [])
    data.setdefault("arg_clusters", [])
    data.setdefault("assignments", [])
    return data


def _call(api_key: str, system: str, user_msg: str) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=MODEL, max_tokens=MAX_TOKENS, system=system,
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    return _parse(text)


def cluster_argpoll(api_key: str, question: str, pairs: list) -> dict:
    """pairs: [{n, claim, justification}]. Ritorna {claim_clusters, arg_clusters, assignments}."""
    return _call(api_key, SYSTEM, _build_user_msg(question, pairs))


def cluster_opentext(api_key: str, question: str, texts: list) -> dict:
    """texts: [{n, text}]. Ritorna {clusters, assignments}."""
    data = _call(api_key, SYSTEM_OPENTEXT, _build_opentext_msg(question, texts))
    data.setdefault("clusters", [])
    return data
