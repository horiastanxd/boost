---
description: Implementeaza strict spec-ul activ; verificare si checkpoint automate.
---

1. `vibe save "pre-build"` - punct de intoarcere.
2. Implementezi STRICT spec-ul activ. Context: spec + HANDOFF.md + SOLUTIONS.md. Stack si comenzi de verificare: campurile `stack`/`verify_hints` din `vibe status --json`.
3. Scrii `verify.sh` cu teste reale pentru fiecare criteriu (fara `vibe verify` inauntru, fara marker de placeholder).
4. `vibe done` - marcheaza, verifica si face checkpoint automat la PASS. Pica -> repari si `vibe verify` din nou (max 3 cicluri, apoi bug report + stop).
5. Raport final: 1-2 propozitii + checklist uman (sau ca e gol).
