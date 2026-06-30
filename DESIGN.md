# Design Doc — RoomPulse

*Strumento di live polling per presentazioni, stile Mentimeter/Slido, su misura per keynote e lezioni di bioetica.*
*Stato: design v1 | Autore: Spit + Ono | Data: 2026-06-26*

> **RoomPulse** — "il polso della sala". Nome composto in stile FakeSpotter (live + sala).
> Path pubblico previsto: `borant.eu/roompulse`.
> Questo file è il *seme* di un repo dedicato: i tool non vivono più dentro Ono3.

---

## 1. Obiettivo e postura

Uno strumento che proietta una domanda, raccoglie le risposte del pubblico via QR/codice,
e mostra i risultati in tempo (quasi) reale. Non un clone generalista di Mentimeter: è
tarato su **presentazioni argomentative su temi etici**, dove conta misurare *posizioni
morali* e *spostamenti di posizione*, non solo fatti.

Tre principi di design che derivano da questa postura:

1. **Il pubblico segue il presenter.** Nessun push: il client del pubblico fa polling e
   mostra qualunque slide il presenter ha reso attiva. Questo dà gratis il *reveal manuale*
   e il controllo scenico.
2. **Aggregabile vs moderabile.** I tipi quantitativi producono un grafico e basta. I tipi
   testuali (open text, Q&A) passano per una coda di moderazione presenter-side.
3. **Frizione zero per il pubblico.** Niente login, niente app. Codice a 5 cifre o QR →
   voti. Token anonimo permissivo (vedi §6).

---

## 2. Decisioni prese (lock)

| Decisione | Scelta |
|---|---|
| Scope domande v1 | 9 tipi: gli 8 base + `argpoll` (claim + justification). Del 9° entra in v1 solo la *raccolta* (feed appaiato piatto); clustering e resa gerarchica rimandati (§10b) |
| Accesso pubblico | Codice + QR su URL pubblico (`borant.eu`) |
| Realtime | Polling leggero (1–2 s), niente websocket |
| Stack | Python (FastAPI) + SQLite |
| Default moderazione | **Visibile** — le risposte testuali appaiono subito, il presenter nasconde i troll |
| Identità voto | **Token anonimo permissivo** — un cookie/localStorage, nessun blocco forte al rivoto |

Decisioni rimandate: vedi §10.

---

## 3. Tipi di domanda

Una sola tabella `Response` con `payload` JSON poliforme regge tutti i tipi. Ogni tipo
serializza la sua forma nel `payload`; la configurazione (opzioni, etichette, min/max)
sta nel `config` della slide.

| Tipo | `slide.type` | `payload` | Aggregazione | Moderazione |
|---|---|---|---|---|
| Multiple choice | `mc` | `{"choice": "opt_id"}` | conteggio per opzione | no |
| Scala / Likert | `scale` | `{"value": 4}` | media + istogramma | no |
| Quadrante 2×2 | `quadrant` | `{"x": 0.3, "y": -0.7}` | nuvola di punti | no |
| Ranking | `ranking` | `{"order": ["a","c","b"]}` | rank medio | no |
| Points (100 pt) | `points` | `{"alloc": {"a": 60, "b": 40}}` | somma per opzione | no |
| Word cloud | `wordcloud` | `{"text": "autonomia"}` | frequenza parole | leggera |
| Open text | `opentext` | `{"text": "..."}` | feed | **sì** |
| Q&A + upvote | `qa` | `{"text": "...", "votes": 0}` | feed ordinato per voti | **sì** |
| Claim + justification | `argpoll` | `{"claim": "...", "justification": "..."}` | **v1:** feed appaiato piatto · **post-v1:** clustering gerarchico (§10b) | **sì** |

### Pre/post
Non è un tipo: è una *relazione* tra due slide gemelle dello stesso tipo, legate da
`pair_id`. La vista risultati le sovrappone e disegna la freccia dello spostamento
(es. media scala da 2.1 → 3.8 dopo l'argomentazione).

### Note per tipo
- **quadrant** — `x` e `y` normalizzati in `[-1, 1]`; le etichette dei 4 poli stanno in `config`.
- **points** — il client valida che la somma sia 100 prima dell'invio.
- **wordcloud** — moderazione "leggera": filtro parole vietate + possibilità di nascondere
  manualmente un token, ma di norma scorre libero.
- **qa** — l'upvote è esso stesso una `Response` figlia? No: per semplicità v1 il contatore
  `votes` vive nel payload della domanda e si incrementa via endpoint dedicato (vedi §5).
- **argpoll** — due campi legati in una sola submission (claim + sua giustificazione). In v1
  riusa l'infrastruttura testuale (raccolta + moderazione + feed) e si rende come card a due
  righe: il claim, e sotto la sua giustificazione. Il raggruppamento semantico dei claim e la
  resa a due livelli sono il pezzo post-v1 descritto in §10b. Il client valida che entrambi i
  campi siano non vuoti.

---

## 4. Modello dati

```sql
CREATE TABLE presentation (
    id            TEXT PRIMARY KEY,        -- uuid
    title         TEXT NOT NULL,
    owner         TEXT NOT NULL,
    join_code     TEXT UNIQUE NOT NULL,    -- 5 cifre, ciò che digita il pubblico
    active_slide  TEXT,                    -- FK slide.id — quale domanda è live ORA
    created_at    TEXT NOT NULL
);

CREATE TABLE slide (
    id               TEXT PRIMARY KEY,
    presentation_id  TEXT NOT NULL REFERENCES presentation(id),
    ord              INTEGER NOT NULL,     -- ordine nella deck
    type             TEXT NOT NULL,        -- mc | scale | quadrant | ranking | points | wordcloud | opentext | qa
    question         TEXT NOT NULL,
    config           TEXT NOT NULL,        -- JSON: opzioni, etichette assi, min/max, parole vietate...
    state            TEXT NOT NULL DEFAULT 'draft',  -- draft | open | closed | revealed
    pair_id          TEXT,                 -- lega due slide gemelle (pre/post)
    UNIQUE (presentation_id, ord)
);

CREATE TABLE response (
    id                 TEXT PRIMARY KEY,
    slide_id           TEXT NOT NULL REFERENCES slide(id),
    participant_token  TEXT NOT NULL,      -- cookie/localStorage anonimo
    payload            TEXT NOT NULL,      -- JSON poliforme per-tipo
    status             TEXT NOT NULL DEFAULT 'visible',  -- visible | hidden | flagged
    created_at         TEXT NOT NULL
);

CREATE INDEX idx_response_slide ON response(slide_id);
```

### Stati della slide
```
draft ──(presenter apre)──> open ──(presenter chiude)──> closed
                              │                              │
                              └──────(reveal)──> revealed <──┘
```
- `open` → il pubblico può votare; i risultati possono essere nascosti o mostrati.
- `revealed` → il pubblico vede i risultati aggregati (per il reveal scenico).
- `active_slide` sulla presentation è ortogonale: dice *cosa proiettare*, lo `state` dice
  *cosa è permesso fare*.

---

## 5. API (FastAPI)

### Lato presenter (autenticato)
```
POST   /api/presentations                      crea deck
GET    /api/presentations/{id}                 dettaglio + slide
POST   /api/presentations/{id}/slides          aggiungi slide
PATCH  /api/slides/{id}                         modifica (question, config, ord, pair_id)
POST   /api/presentations/{id}/activate         body: {slide_id} → set active_slide
PATCH  /api/slides/{id}/state                   body: {state: open|closed|revealed}
GET    /api/slides/{id}/responses               coda moderazione (tutte, anche hidden)
PATCH  /api/responses/{id}/status               body: {status: visible|hidden|flagged}
```

### Lato pubblico (anonimo, via join_code)
```
GET    /api/live/{join_code}                    → {active_slide, type, question, config, state}
                                                  ← il polling 1–2 s batte qui
POST   /api/live/{join_code}/respond            body: {slide_id, payload} (+ token in cookie)
POST   /api/live/{join_code}/upvote             body: {response_id}  (solo qa)
GET    /api/live/{join_code}/results            → aggregato della slide attiva
                                                  (serve solo i risultati se state=revealed/open)
```

### Aggregazione
Calcolata on-the-fly da `response` per la slide attiva — niente tabelle di rollup nel v1
(i volumi non lo giustificano). La forma dell'aggregato dipende dal tipo (conteggi,
istogramma, nuvola di punti, frequenze).

---

## 6. Identità e anti-abuso (postura permissiva)

- Al primo accesso il client genera un `participant_token` (uuid in localStorage + cookie).
- Il token viaggia con ogni voto. **Non** impedisce il rivoto da un altro browser: scelta
  consapevole — in aula il rischio è basso e la frizione di un blocco forte non vale.
- Difese leggere v1: una risposta per `(slide_id, participant_token)` sui tipi a voto singolo
  (upsert), rate-limit per IP sugli endpoint di scrittura, filtro parole vietate sui tipi
  testuali. Nessun captcha, nessun login.

---

## 7. Le tre superfici

| Superficie | Accesso | Cosa fa |
|---|---|---|
| **Editor** | privato | costruisci la deck, ordini le slide, definisci config e coppie pre/post |
| **Presenter / proiezione** | privato | vista grande: domanda attiva + risultati live + controlli open/close/reveal + coda moderazione + QR e join_code |
| **Audience** | pubblico (`/`) | codice/QR → segue `active_slide` → vota → vede i risultati se rivelati |

La proiezione è la superficie con più cura visiva: è ciò che sta sullo schermo dietro di te.
Il quadrante e il pre/post sono i due momenti "wow" da curare (nuvola di punti animata,
freccia dello spostamento).

---

## 8. Stack e deploy

- **Backend:** FastAPI + SQLite (file), gestito con `uv`. Coerente con `survey.borant.eu`.
- **Frontend:** statico servito da FastAPI. Vanilla JS o un micro-framework leggero;
  il polling è una `fetch` ogni 1–2 s su `/api/live/{code}`.
- **QR:** generato server-side (`qrcode`) puntando a `borant.eu/roompulse/?c=<join_code>`.
- **Deploy:** stesso pattern di `survey.borant.eu` (reverse proxy + servizio Python).
- **Backup:** copia del file SQLite.

---

## 9. Roadmap implementativa (proposta)

1. **Schema + migrazioni** SQLite, modelli Pydantic per i payload per-tipo.
2. **API live + polling**: `activate`, `respond`, `results` con i tipi `mc` e `scale`.
   È lo scheletro che dimostra il loop presenter→pubblico→risultati.
3. **Editor minimale** per creare deck e slide.
4. **Proiezione** con grafici per `mc` e `scale`.
5. **Tipi restanti** uno alla volta: wordcloud → quadrant → ranking → points.
6. **Layer testuale + moderazione**: opentext, qa, coda presenter.
7. **`argpoll` — input appaiato** claim + justification: raccolta, moderazione, feed appaiato
   piatto (card a due righe). Riusa il layer del passo 6. *Il clustering e la resa gerarchica
   NON entrano qui* (vedi §10b).
8. **Pre/post**: `pair_id` e vista sovrapposta.
9. **Rifinitura visiva proiezione** (animazioni, QR, transizioni).

---

## 10b. Mentimeter++ — clustering e resa gerarchica (post-v1)

L'idea originale di Holger Baumann (meeting 19.06) è la *ragione genetica* del tool. Si
decompone in tre pezzi, e la linea di taglio v1/post-v1 cade *dentro* di essa:

- **Input appaiato `claim + justification`** → **in pipeline v1** (passo 7 della roadmap).
  La raccolta delle coppie e il feed appaiato piatto entrano subito, riusando il layer testuale.
- **Clustering semantico LLM** → **post-v1.** Lo penseremo dopo.
- **Resa gerarchica cluster → giustificazioni** → **post-v1**, perché dipende dal clustering:
  senza cluster non c'è gerarchia da rendere.

Il caso d'uso di Baumann: *"Quale criterio usare nelle decisioni di triage? E quale la
giustificazione?"* — lo studente immette un **claim** (il criterio) **+** la sua
**justification**; in v1 le coppie si raccolgono e si mostrano piatte; più avanti l'LLM
clusterizza i claim per significato e annida le giustificazioni sotto ciascun cluster. Payoff
didattico: mostrare la diversità dei criteri e che le giustificazioni spesso non sono *etiche*
o non chiudono.

I due pezzi rimandati, in dettaglio:

1. **Clustering semantico LLM** — l'aggregazione *non* è on-the-fly: l'LLM raggruppa i claim per significato (non per token, quindi il word cloud non basta) e conta. Output: cluster → [giustificazioni].
2. **Resa a due livelli** — la proiezione mostra i cluster-claim ordinati per frequenza, con le giustificazioni annidate sotto. Tutti i risultati v1 (incluso il feed `argpoll` di v1) sono piatti.

**Implicazione architetturale** — il clustering rompe l'assunzione di §5 ("aggregazione
on-the-fly, niente rollup"). È costoso, lento, non-deterministico: va **materializzato e
cachato**. Serve una tabella `cluster` (o aggregato salvato) che il presenter rigenera *a
comando* ("clusterizza ora"), non a ogni poll. È l'unico punto in cui questo layer tocca lo
schema base, quindi vale tenerlo presente già ora anche se non lo si implementa. Il payload
`argpoll` (`{claim, justification}`) è già la forma giusta su cui il clustering opererà.

```sql
-- bozza, post-v1
CREATE TABLE cluster (
    id           TEXT PRIMARY KEY,
    slide_id     TEXT NOT NULL REFERENCES slide(id),
    label        TEXT NOT NULL,        -- etichetta del claim-cluster, generata dall'LLM
    ord          INTEGER NOT NULL,     -- per frequenza
    generated_at TEXT NOT NULL         -- quando il presenter ha lanciato il clustering
);
-- ogni response argpoll riceve un cluster_id alla materializzazione
ALTER TABLE response ADD COLUMN cluster_id TEXT;  -- NULL finché non clusterizzata
```

Possibile sinergia: il layer LLM-su-testo esiste già in AutoCode/AutoMap; quando si arriva
qui, valutare se riusare quel codice invece di riscriverlo.

## 10. Decisioni rimandate

- ~~**Naming** del tool e del path pubblico.~~ → **RoomPulse**, `borant.eu/roompulse`.
- **Persistenza storica**: una deck è riusabile in più sessioni? Servono "run" separate
  che archiviano le risposte per sessione? (probabile sì in v1.1)
- **Export** dei risultati (CSV/PNG) per riuso nei paper/slide.
- **Tema visivo** della proiezione (allineato al tuo stile keynote?).
- **Limite parole vietate**: lista statica o editabile per-slide?
