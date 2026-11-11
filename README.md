# NuPhy Air75 HE sous Linux

Tous les réglages du driver web NuPhyIO (touches, point d'actionnement, rapid
trigger, touches avancées, éclairage, macros, modes) dans une application
locale, sans navigateur ni connexion.

| Fichier | Rôle | Dépendances |
|---|---|---|
| `nuphy.py` | bibliothèque + ligne de commande | Python seul (≥ 3.10) |
| `nuphy-gui.py` | fenêtre de réglages | GTK 4 + libadwaita ≥ 1.5 |
| `nuphy_data.json` | modèles, dispositions, codes de touches | — |
| `nuphy_sim.py` | clavier simulé (tests et mode démo) | — |
| `70-nuphy-he.rules` | règle udev | — |

> **État : pas encore essayé sur un vrai clavier.** Le protocole vient d'une
> lecture du driver web, pas d'une capture USB. Ce qui est vérifié : mes trames
> et encodages sont identiques, octet pour octet, à ceux que produit le code du
> driver (ses propres sérialiseurs, exécutés isolément), et l'ensemble tourne
> contre un clavier simulé construit sur les données du « clavier de démo » que
> NuPhy embarque dans son driver (un Air75 HE, firmware 1.0.6). Voir
> [Premier essai](#premier-essai-sur-le-vrai-clavier) avant de modifier quoi que
> ce soit.

*Projet non officiel, sans lien avec NuPhy. Les noms NuPhy et NuPhyIO
appartiennent à leur propriétaire et ne servent ici qu'à désigner le matériel.*

## Installer

```sh
sudo pacman -S --needed python python-gobject gtk4 libadwaita
```

```sh
./install.sh
```

Pas de root : liens dans `~/.local/bin`, entrée « Clavier NuPhy HE » dans le
lanceur. `./install.sh --uninstall` retire tout.

### Règle udev (demande sudo, une fois)

Sans règle, seul root peut parler au clavier. `70-nuphy-he.rules` ouvre à la
personne connectée **uniquement l'interface de configuration** (interface USB
n° 1, celle qu'utilise le driver web) ; les interfaces par lesquelles passe ce
que tu tapes ne sont pas touchées.

```sh
sudo install -m 644 70-nuphy-he.rules /etc/udev/rules.d/
```

```sh
sudo udevadm control --reload && sudo udevadm trigger
```

## Premier essai sur le vrai clavier

Dans cet ordre, du plus anodin au moins anodin :

```sh
nuphy info
```

Doit afficher le modèle, le firmware et le mode actif. Ce sont deux lectures.

```sh
nuphy --debug show
```

Lit les réglages du mode et montre chaque trame. Si une lecture échoue ici,
arrête-toi là et garde la sortie : c'est ce qu'il me faut pour corriger.

```sh
nuphy export avant.json --all
```

Sauvegarde complète des trois modes, au format du driver web (réimportable ici
comme sur drive.nuphy.io). Toujours des lectures.

```sh
nuphy light main --brightness 30
```

Première écriture : un seul octet, visible tout de suite, sans risque. Chaque
écriture est relue dans le clavier ; si la commande ne dit pas « erreur », le
clavier a pris la valeur.

Avant la toute première modification, l'outil enregistre aussi de lui-même une
sauvegarde complète dans `~/.local/state/nuphy-he/config-origine-*.json`.

## La fenêtre

```sh
nuphy-gui
```

Le sélecteur en haut à gauche choisit lequel des trois modes du clavier (M1
Gaming, M2 Windows, M3 Mac) tu règles ; au démarrage c'est le mode actif.

| Onglet | Ce qu'il fait |
|---|---|
| Touches | réassigner chaque touche sur les 4 couches (base, Fn, Fn 2, couche 3) : clavier, média, souris, fonctions RGB, macros ; remise d'usine d'une touche ou d'une couche |
| Actionnement | par touche ou par groupe : type de switch, point d'actionnement, Rapid Trigger (simple ou continu), sensibilités d'appui et de relâchement, zones mortes haute et basse |
| Touches avancées | Mod-Tap, Bascule, DKS (4 actions par profondeur), Rapid Shift, SOCD (4 règles de priorité), Hyper Tap |
| Éclairage | rétroéclairage et lumière latérale : effet, luminosité, vitesse, sens, couleur ou arc-en-ciel ; couleur par touche (effet Custom) |
| Macros | 32 macros par mode, saisies ou enregistrées au clavier, assignées à une touche (une fois, n fois, tant que tenue, jusqu'au prochain appui) |
| Mode et réglages | nom du mode, polling 1000-8000 Hz, tick rate, anti-rebond, mode Tachyon, verrous Win / Alt+Tab / Alt+F4, veille, manette virtuelle, export / import, remise d'usine |
| Outils | course des touches en direct, recalibration des switches |

Pour voir l'interface sans clavier (rien n'est envoyé nulle part) :

```sh
NUPHY_DEMO=1 nuphy-gui
```

## Ligne de commande

`-m 1|2|3` choisit le mode (défaut : le mode actif), `-l 0-3` la couche.

```sh
nuphy info                                   # modèle, firmware, mode actif
nuphy show                                   # réglages du mode
nuphy keys -l 1                              # carte de la couche Fn
nuphy keys --names                           # noms des touches physiques
nuphy codes vol                              # codes assignables contenant « vol »
nuphy key caps esc                           # Verr.Maj envoie Échap
nuphy key caps default                       # retour à l'usine
nuphy trigger                                # point d'actionnement de chaque touche
nuphy trigger w,a,s,d --ap 0.4 --rt continu --press 0.1 --release 0.1
nuphy trigger all --ap 1.2 --rt off --top 0.1 --bottom 0.1
nuphy light main --effect wave --brightness 60 --speed 3 --color ff8800 --rainbow off
nuphy light side --effect static --color 00aaff
nuphy color w,a,s,d ff0000                   # couleur par touche (effet custom)
nuphy rate 8000                              # polling USB
nuphy tick 60 ; nuphy debounce mid ; nuphy lock win on ; nuphy tachyon on
nuphy sleep 15                               # minutes, 0 = jamais
nuphy rename "Jeu"
nuphy adv mt caps esc lctl --ms 200          # bref : Échap, maintenu : Ctrl
nuphy adv toggle tab lsft
nuphy adv socd a d --rule neutral            # last, neutral, a, b
nuphy adv rs a d ; nuphy adv ht q e --one-way --travel 0.5
nuphy adv dks f a:hr.. b:..t. --points 1,3,3,1
nuphy adv list ; nuphy adv remove a
nuphy macro set 1 "+lctl +c 20 -c -lctl"     # +appui, -relâchement, nombre = délai ms
nuphy macro bind f12 1 ; nuphy macro list ; nuphy macro delete 1
nuphy gamepad --type classic --square-gate on   # firmware ≥ 1.1.1
nuphy calibrate ; nuphy monitor
nuphy export mode.json ; nuphy -m 3 import mode.json
nuphy reset --yes ; nuphy reset --factory --yes
```

Les phases d'une action DKS sont quatre lettres, une par étape de la course
(début d'appui, fond, relâchement depuis le fond, relâchement complet) :
`t` appui bref, `h` touche enfoncée et tenue, `r` relâchée (après un `h`),
`.` rien.

## Ce qui n'est pas repris du driver web

- **La mise à jour du firmware.** Volontairement : c'est la seule opération qui
  peut rendre le clavier inutilisable, et NuPhy ne la valide que dans son
  driver. L'outil refuse même d'émettre la trame qui fait entrer le clavier en
  mode mise à jour.
- Les « configurations recommandées » téléchargées depuis les serveurs NuPhy.
- L'éditeur de courbe de la manette virtuelle (la bibliothèque sait lire et
  écrire la table, sans interface).
- Les groupes de touches mémorisés de la page d'actionnement (la sélection
  multiple les remplace).

Les légendes des touches sont celles d'un clavier US : le clavier envoie des
positions, c'est la disposition choisie dans le système qui en fait des
caractères.

## Sécurité

- Rien n'écrit de firmware. L'en-tête des blocs de réglages, par lequel le
  driver web déclenche le mode mise à jour, est interdit en écriture dans la
  bibliothèque, quel que soit l'appelant.
- Les trames envoyées sont celles du driver web. Chaque écriture est relue dans
  le clavier ; une commande qui se termine sans erreur a réellement changé le
  clavier.
- Réponses vérifiées : commande, adresse et somme de contrôle. Une réponse
  douteuse est redemandée, jamais interprétée.
- Un verrou (`$XDG_RUNTIME_DIR/nuphy-he.lock`) empêche la fenêtre et la ligne
  de commande de parler au clavier en même temps. Ferme l'onglet du driver web
  avant d'utiliser l'outil : deux programmes sur la même interface se
  mélangent les réponses.
- Les touches « Bootloader » et « Reset clavier » existent dans le driver et
  donc ici ; la fenêtre demande confirmation avant de les assigner.

## Autres claviers HE

La table du driver décrit aussi les Air60 HE, Field75 HE (V1 et V2), Halo65 HE
(et Pro), BH65 HE et Gem80 HE, qui partagent ce protocole. Ils sont reconnus,
mais seul l'Air75 HE dispose de données de référence dans les tests ;
`nuphy info` le signale. Les claviers mécaniques NuPhy (Air V3, Halo, Node…)
parlent un autre protocole et ne sont pas gérés.

## Le protocole, en bref

Interface USB 1, collection vendeur (usage page 1, usage 0), rapports de
64 octets en entrée et en sortie, sans numéro de rapport.

```
requête  55 cmd 00 somme lg adr_lo adr_hi 00 données…    somme = octets 4 à 63
réponse  aa cmd 00 somme lg adr_lo adr_hi 00 données…
```

Chaque réglage vit dans une zone mémoire adressée par (commande, adresse) ;
56 octets au plus par trame. Un rapport qui ne commence pas par `aa` est un
événement (changement de mode, course d'une touche…). Pas de commande de
validation : une écriture est active et persistante aussitôt.

| Lire / écrire | Zone | Taille |
|---|---|---|
| `03` | identité : version du firmware, date de compilation | — |
| `04` | mode actif, couches par mode | — |
| `05` / `06` | réglages et éclairage d'un mode | 64 par mode |
| `07` | carte des touches d'usine | 512 par couche, 12 couches |
| `08` / `09` | carte des touches (3 octets par touche : type, code0, code1) | idem |
| `0a` / `0b` | couleur par touche | 512 par mode |
| `0c` / `0d` | macros (table de 32 pointeurs, puis actions de 4 octets) | 2048 par mode |
| `10` / `11` | manette virtuelle | 256 |
| `a0` / `a1` | actionnement et rapid trigger (8 octets par touche) | 1024 par mode |
| `a2` / `a3` | DKS (32 × 24 octets) | 768 par mode |
| `a4` / `a5` | Mod-Tap, RS, SOCD, HT (32 × 6 octets) | 256 par mode |
| `a6` / `a7` | Bascule (32 × 3 octets) | 128 par mode |
| `a8`, `a9` | début et fin de calibration | — |
| `f1` / `f2` | données de l'application (noms des modes…) | 1024 |
| `01`, `02` | début et fin du mode de transfert rapide | — |
| `ee` | remise d'usine | — |

Bloc de réglages d'un mode : octet 4 polling (bits 0-3 : 1 = 8000 Hz …
4 = 1000 Hz) et tick rate (bits 4-7) ; octet 5 veille (bit 7 activée,
bits 0-6 minutes) ; octet 6 verrous Win, Alt+Tab, Alt+F4 ; octet 7 options et
anti-rebond ; octets 8-16 rétroéclairage (effet, luminosité, vitesse, sens,
arc-en-ciel, index, R, V, B) ; octets 24-31 lumière latérale.

Actionnement d'une touche (8 octets) : switch (5 bits) ; mode (0 normal, 1 RT,
2 RT continu) et priorité SOCD ; course sur 9 bits ; puis deux mots de 16 bits,
sensibilité sur 9 bits et zone morte sur 7. Unités : (valeur + 1) × 0,01 mm sur
l'Air75 HE.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

```sh
NUPHY_DEMO=1 xvfb-run -a python3 tests/gui_smoke.py
```

Le premier compare les encodages aux vecteurs de référence
(`tests/vendor_vectors.json`, sorties des sérialiseurs du driver) et exerce la
bibliothèque et la ligne de commande contre le clavier simulé. Le second ouvre
la fenêtre, utilise chaque onglet et vérifie ce qui est arrivé au clavier.

## Sources

Le driver web NuPhyIO (drive.nuphy.io, bundle `main.ba072d08.js` du
11 novembre 2026), et les notes de
[marksveizgut/nuphy-kde](https://github.com/marksveizgut/nuphy-kde) (descripteur
de l'interface de configuration relevé sur un Air75 HE),
[Gloridust/NuphyioDesktop](https://github.com/Gloridust/NuphyioDesktop) et
[fldc/nuphyctl](https://github.com/fldc/nuphyctl) (protocole des claviers
mécaniques, méthode d'analyse).

## Licence

MIT, voir `LICENSE`.
