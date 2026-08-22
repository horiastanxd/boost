---
description: Ruleaza lantul vibe complet - stare, spec, build, verify, checkpoint.
---

1. Ruleaza `vibe status --json` si urmeaza starea, fara opriri intre pasi.
2. `empty_project`/`no_spec`: scrii TU spec-ul (vezi workflow-ul spec), userul zice da/nu, apoi `vibe activate <nume>` si continui direct cu build.
3. `spec_unimplemented`: build direct (vezi workflow-ul build).
4. `build_unverified`: `vibe verify`; repari ce pica si repeti (max 3 cicluri).
5. `verified_ok`: anunti in 1-2 propozitii + checklist uman. `bug_report`: continui fixul. `context_high`: scrii HANDOFF.md si ceri sesiune noua.

Porti umane permise: ideea, aprobarea spec (da/nu), sesiune noua. Restul decizi singur, conservator.
