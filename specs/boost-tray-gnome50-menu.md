## Ce face

Schimba categoria indicatorului Boost din `SYSTEM_SERVICES` in
`APPLICATION_STATUS`, astfel incat GNOME sa il trateze ca indicator cu meniu
propriu. Documenteaza diagnosticul si workaround-ul pentru bug-ul extensiei
AppIndicator de pe Ubuntu 26.04 / GNOME 50. Reinstaleaza local aplicatia si
verifica faptul ca procesul tray a fost repornit.

## Ce NU face

- Nu extinde scopul fara spec noua.

## Presupuneri

Deciziile luate de agent fara sa intrebe. Userul poate corecta oricare cu un "nu".

- Schimbarea de categorie este suficienta pentru a separa meniul Boost de
  Quick Settings; daca nu, extensia AppIndicator instalata necesita actualizare.
- Reinstalarea locala prin `sudo ./install.sh` este autorizata de cererea
  utilizatorului si afecteaza doar instalarea locala Boost.

## Criterii de acceptare

Fiecare criteriu e verificat de agent printr-o comanda in verify.sh.

- `lib/boost-tray.py` se parseaza cu `ast.parse`.
- Indicatorul foloseste `APPLICATION_STATUS` si nu mai foloseste
  `SYSTEM_SERVICES`.
- Documentatia include simptomul, fix-ul si referinta la issue-ul upstream.
- `sudo ./install.sh` reuseste, iar `boost-tray` ruleaza din nou.

## Constrangeri

- Nu atinge zone nelegate de acest feature.

## Estimare sesiuni

1

## Checklist uman

Doar ce e strict imposibil de verificat automat, cu motivul in paranteza. Ideal: gol.

- Click pe iconita Boost deschide meniul propriu; necesita interactiune GNOME
  vizuala dupa reinstalare.
