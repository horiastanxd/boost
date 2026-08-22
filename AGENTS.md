# Reguli vibe (Codex / Antigravity / orice agent)

Aici nu ruleaza hook-urile din Claude Code. Tu aplici regulile mecanic, prin CLI.
`vibe` e in PATH; fallback: `~/.vibe/bin/vibe`.

- Inceput de sesiune: ruleaza `vibe status --json` si urmeaza starea. Nu ghici.
- Lant automat (echivalent /go): no_spec -> scrii TU spec-ul (draft cu Presupuneri, userul zice doar da/nu) -> `vibe activate <nume>` -> build -> `vibe done`.
- Fara spec activa nu scrii cod. Idei noi in `ideas/`.
- Comenzi build/test/run: prin `vibe loopstop <comanda>` - taie output la 80 linii si opreste buclele (3 erori identice -> bug report + stop).
- "Gata" = `vibe done` a trecut (marcheaza + verifica + checkpoint automat). Niciodata altfel.
- verify.sh acopera FIECARE criteriu de acceptare; checklist uman doar imposibil-de-automatizat, max 3 itemi, cu motiv.
- Economie: `rg -n` inainte de citire, citire pe intervale, fara recitiri, output lung prin `| tail -50`, raspunsuri scurte.
