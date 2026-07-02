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

SYSTEM = """You are an argumentation analyst. You receive a numbered list of responses, each containing a CLAIM (a proposed criterion) and a JUSTIFICATION (the reason given in support of it).

Produce TWO distinct groupings, both DESCRIPTIVE and never evaluative:

1. claim_clusters — group the CLAIMS by substantive criterion. The label should express the criterion in a short, neutral form, for example: "Maximize aggregate benefit", "Protect vulnerable people".
2. arg_clusters — group the JUSTIFICATIONS by THE TYPE OF CONSIDERATION THEY APPEAL TO, NOT by the claim they support. The label should be thematic and descriptive, using the form "Appeal to…", for example: "Appeal to impartiality", "Appeal to consequences", "Appeal to protecting vulnerable people", "Appeal to social contribution". The labels should describe what the argument appeals to without judging its validity. NEVER use evaluative terms such as "fallacious", "circular", "weak", "unethical", or "valid". Aim for 4–8 groups.

The same criterion may be supported by different kinds of appeals, and the same kind of appeal may support different criteria. This is normal and intentional.

Then assign EACH response, identified by its number, to exactly one claim_cluster and exactly one arg_cluster.

Write all labels in the same language as the responses. Do not infer the output language from this prompt. If the responses are in English, use English labels; if they are in another language, use that language.
Return ONLY valid JSON, with no text before or after it, using exactly this format:
{
  "claim_clusters": [{"id": "c1", "label": "..."}],
  "arg_clusters": [{"id": "a1", "label": "..."}],
  "assignments": [{"n": 1, "claim": "c1", "arg": "a1"}]
}"""


SYSTEM_OPENTEXT = """You are a thematic response analyst. You receive a QUESTION and a numbered list of free-text RESPONSES.

Your task is to group free-text answers to a question into THEMATIC clusters that are descriptive and never evaluative.
You receive the QUESTION that was asked and a numbered list of RESPONSES.

Group the responses by their SUBSTANTIVE TOPIC: the main issue, consideration, proposal, concern, reason, or perspective addressed by each response in relation to the question.

Produce:

* clusters — group responses by their main substantive topic in relation to the question. The label describes the shared topic in a short, neutral form, in the same language as the responses. Group by what the response is about, not by wording, tone, sentiment, agreement, or level of detail. NEVER use evaluative terms such as "fallacious", "circular", "weak", "valid", "unethical", "right", or "wrong". Aim for 4–8 groups.
* assignments — assign EACH response, identified by its number, to exactly one cluster. If a response covers multiple topics, assign it according to its main or most explicit focus.

Write all labels in the same language as the responses.

Return ONLY valid JSON, with no text before or after it, using exactly this format:
{
  "clusters": [{"id": "t1", "label": "..."}],
  "assignments": [{"n": 1, "cluster": "t1"}]
}"""


def _build_user_msg(question: str, pairs: list) -> str:
    lines = [f"QUESTION: {question}", "", "RESPONSES:"]
    for p in pairs:
        lines.append(f'{p["n"]}. CLAIM: {p["claim"]} | JUSTIFICATION: {p["justification"]}')
    return "\n".join(lines)


def _build_opentext_msg(question: str, texts: list) -> str:
    lines = [f"QUESTION: {question}", "", "RESPONSES:"]
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
    """pairs: [{n, claim, justification}]. Return {claim_clusters, arg_clusters, assignments}."""
    return _call(api_key, SYSTEM, _build_user_msg(question, pairs))


def cluster_opentext(api_key: str, question: str, texts: list) -> dict:
    """texts: [{n, text}]. Return {clusters, assignments}."""
    data = _call(api_key, SYSTEM_OPENTEXT, _build_opentext_msg(question, texts))
    data.setdefault("clusters", [])
    return data
