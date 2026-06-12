# Prompt: sintesi su richiesta

Quando l'utente fa una domanda di conoscenza su un progetto e la risposta
merita di essere conservata, scrivi la sintesi come pagina wiki:

1. Raccogli le pagine pertinenti (index.md → sources/entities/concepts,
   oppure `wikibridgectl.py search "<query>"`).
2. Componi una risposta esaustiva in markdown con i riferimenti alle
   pagine usate come link `[[sources/x.md]]`.
3. Salvala in `wiki/syntheses/<slug-domanda>.md` con frontmatter:

```yaml
---
title: <domanda riformulata>
kind: synthesis
updated: <ISO 8601>
---
```

4. Esegui `wikibridgectl.py index` per aggiornare indice e FTS.
