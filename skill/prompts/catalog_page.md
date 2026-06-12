# Prompt: catalogazione di una pagina

Leggi la source indicata nel task (`data/projects/<slug>/wiki/<source>`)
ed estrai la conoscenza in questo JSON (e SOLO questo JSON):

```json
{
 "entities": [
  {"name": "...", "type": "persona|organizzazione|sistema|luogo|prodotto",
   "summary": "1-3 frasi esaustive su cosa dice il testo di questa entità"}
 ],
 "concepts": [
  {"name": "...", "summary": "1-3 frasi che spiegano il concetto come appare nel testo"}
 ],
 "relations": [
  {"from": "...", "to": "...", "kind": "verbo o relazione breve"}
 ]
}
```

Criteri:
- Solo elementi sostanziali: persone, organizzazioni, sistemi, prodotti,
  concetti tecnici o di dominio. Niente parole generiche.
- I summary devono essere autosufficienti (leggibili senza la source).
- `relations` collega per nome esatto due elementi presenti in
  entities/concepts.

Applica il risultato con:

```bash
python3 /mnt/c/Projects/wikibridge/skill/wikibridgectl.py catalog-apply <source-path> --json '<json>' [--project <slug>]
```
