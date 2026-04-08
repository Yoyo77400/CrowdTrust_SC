# CrowdTrust - Analyse du Smart Contract

## Partie 1 : Entrypoints et Tests

### Entrypoints (7)

| # | Entrypoint | Params | Role | Acces |
|---|-----------|--------|------|-------|
| 1 | `create_pot` | title, description, goal, deadline, validation_mode | Cree une nouvelle cagnotte (statut Active) | Ouvert |
| 2 | `contribute` | pot_id (+ XTZ envoyes) | Contribue a une cagnotte active. Cumule les contributions par adresse. Passe automatiquement en statut Funded quand l'objectif est atteint | Ouvert |
| 3 | `vote` | pot_id, approve | Vote pondere (poids = montant contribue) pour ou contre la liberation des fonds. Uniquement en mode Vote (validation_mode=0) | Contributeurs uniquement |
| 4 | `release` | pot_id | Libere les fonds vers le createur apres vote favorable ou en mode auto | Createur uniquement |
| 5 | `claim_refund` | pot_id | Permet a un contributeur de retirer ses fonds si la cagnotte est Failed ou Cancelled | Contributeurs uniquement |
| 6 | `cancel` | pot_id | Annule une cagnotte Active ou Funded | Createur uniquement |
| 7 | `refund` | pot_id | Marque une cagnotte comme Failed (deadline depassee ou vote negatif) | Ouvert |

### Machine a etats

```
Active (0) --[objectif atteint]--> Funded (1) --[release]--> Released (2)
    |                                  |
    |--[deadline depassee]-->    Failed (3) <--[vote negatif]--|
    |                                  ^
    |--[cancel]-->          Cancelled (4)
                                  ^
                  Funded (1) --[cancel]--|
```

---

### Tests unitaires (7 suites, ~41 cas)

#### 1. `test_create_pot` - 5 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Creation reussie avec parametres valides | Happy path | Pot cree, status=0, creator correct |
| 2 | ID auto-incremente | Happy path | next_id == 2 |
| 3 | Echec si goal = 0 | Erreur | `GOAL_MUST_BE_POSITIVE` |
| 4 | Echec si deadline dans le passe | Erreur | `DEADLINE_MUST_BE_FUTURE` |
| 5 | Echec si validation_mode invalide (5) | Erreur | `INVALID_VALIDATION_MODE` |

#### 2. `test_contribute` - 9 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Contribution reussie | Happy path | total_contributed mis a jour |
| 2 | Contributions multiples du meme user (cumul) | Happy path | Montants cumules |
| 3 | Verification total_contributed | Happy path | Somme correcte |
| 4 | Auto-transition vers Funded | Happy path | status == 1 |
| 5 | Echec si montant = 0 | Erreur | `CONTRIBUTION_MUST_BE_POSITIVE` |
| 6 | Echec si pot inexistant | Erreur | `POT_NOT_FOUND` |
| 7 | Echec si deadline depassee | Erreur | `DEADLINE_PASSED` |
| 8 | Auto-transition en mode auto | Happy path | status == 1 |
| 9 | Echec si pot deja Funded | Erreur | `POT_NOT_ACTIVE` |

#### 3. `test_vote` - 7 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Vote favorable enregistre | Happy path | vote_for incremente |
| 2 | Vote defavorable enregistre | Happy path | vote_against incremente |
| 3 | Cumul et poids des votes | Happy path | Totaux corrects |
| 4 | Echec si pas contributeur | Erreur | `NOT_A_CONTRIBUTOR` |
| 5 | Echec si double vote | Erreur | `ALREADY_VOTED` |
| 6 | Echec si pot pas Funded | Erreur | `POT_NOT_FUNDED` |
| 7 | Echec si mode auto | Erreur | `POT_IS_AUTO_MODE` |

#### 4. `test_release` - 6 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Release apres vote majoritaire favorable | Happy path | status == 2, fonds envoyes |
| 2 | Release en mode auto (sans vote) | Happy path | status == 2 |
| 3 | Echec si vote defavorable | Erreur | `VOTE_NOT_FAVORABLE` |
| 4 | Echec si non-createur | Erreur | `NOT_CREATOR` |
| 5 | Echec si pas en statut Funded | Erreur | `POT_NOT_FUNDED` |
| 6 | Echec si deja Released | Erreur | `POT_NOT_FUNDED` |

#### 5. `test_claim_refund` - 6 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Remboursement sur Failed | Happy path | Fonds rendus |
| 2 | Remboursement sur Cancelled | Happy path | Fonds rendus |
| 3 | Echec si pot Active | Erreur | `REFUND_NOT_AVAILABLE` |
| 4 | Echec si pot Released | Erreur | `REFUND_NOT_AVAILABLE` |
| 5 | Echec si pas contributeur | Erreur | `NOT_A_CONTRIBUTOR` |
| 6 | Echec si double remboursement | Erreur | `ALREADY_REFUNDED` |

#### 6. `test_cancel` - 4 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Annulation par le createur | Happy path | status == 4 |
| 2 | Echec si non-createur | Erreur | `NOT_CREATOR` |
| 3 | Echec si deja Released | Erreur | `CANNOT_CANCEL` |
| 4 | Echec si deja Cancelled | Erreur | `CANNOT_CANCEL` |

#### 7. `test_refund` - 4 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Deadline depassee -> Failed | Happy path | status == 3 |
| 2 | Vote negatif -> Failed | Happy path | status == 3 |
| 3 | Echec si deadline non depassee | Erreur | `DEADLINE_NOT_PASSED` |
| 4 | Echec si deja Failed | Erreur | `REFUND_NOT_APPLICABLE` |

### Scenarios End-to-End (4)

| # | Scenario | Flux |
|---|---------|------|
| 1 | `test_e2e_success` | Create -> Contribute (x3, objectif atteint) -> Vote OUI majoritaire -> Release |
| 2 | `test_e2e_deadline_failure` | Create -> Contributions insuffisantes -> Deadline -> Refund -> Claim refund |
| 3 | `test_e2e_cancel` | Create -> Contribute -> Cancel -> Claim refund |
| 4 | `test_e2e_negative_vote` | Create -> Contribute (x3, objectif atteint) -> Vote NON majoritaire -> Release echoue -> Refund -> Claim refund |

---

## Partie 2 : Ameliorations possibles et Failles potentielles

### Failles et risques de securite

#### 1. Vote ploutocratique (Severite : MOYENNE)

**Probleme** : Le poids du vote est directement proportionnel au montant contribue. Un contributeur qui apporte 51% des fonds peut a lui seul approuver la liberation, meme si tous les autres votent contre.

**Exemple** : Alice contribue 600 XTZ, Bob 200 XTZ, Charlie 200 XTZ (total = 1000 XTZ, goal atteint). Alice vote OUI (600), Bob et Charlie votent NON (400). Quorum atteint (100% > 50%), vote_for (600) > vote_against (400) -> Release approuve malgre 2 contributeurs sur 3 opposes.

**Impact** : Collusion possible entre createur et un gros contributeur. Le createur peut contribuer a sa propre cagnotte pour s'assurer la majorite du vote.

**Correction possible** : Offrir un mode de vote "1 personne = 1 voix" ou un vote quadratique (poids = racine carree du montant).

---

#### 2. Createur peut contribuer a sa propre cagnotte (Severite : MOYENNE)

**Probleme** : Rien n'empeche le createur de contribuer a sa propre cagnotte. Combine avec le vote ploutocratique, le createur peut :
1. Creer une cagnotte de 100 XTZ
2. Contribuer 51 XTZ lui-meme
3. Attendre d'autres contributions
4. Voter OUI avec son propre poids
5. Release les fonds (y compris ceux des autres)

**Impact** : Le createur recupere l'argent des autres contributeurs en plus du sien. C'est une forme de self-dealing.

**Correction possible** : Interdire au createur de contribuer (`assert sp.sender != pot.creator`) ou exclure sa contribution du poids de vote.

---

#### 3. Fonds bloques si le quorum n'est jamais atteint (Severite : MOYENNE)

**Probleme** : En mode vote, si la cagnotte est Funded mais que les contributeurs ne votent pas (ou pas assez pour atteindre 50% de quorum), les fonds restent bloques indefiniment. Ni `release` (quorum non atteint) ni `refund` (quorum non atteint) ne peuvent passer.

**Exemple** : 10 contributeurs, seulement 2 votent (20% < 50% quorum). Impossible de release ni de refund. Fonds bloques a jamais dans le contrat.

**Impact** : Perte definitive de fonds pour tous les contributeurs.

**Correction possible** : Ajouter un delai de vote (vote_deadline) apres lequel les fonds sont automatiquement refundables, ou permettre au createur de cancel meme apres Funded (deja le cas dans le contrat actuel via `cancel` qui accepte status <= 1).

---

#### 4. Egalite de vote traitee comme echec (Severite : FAIBLE)

**Probleme** : La condition de release est `vote_for > vote_against` (strictement superieur). En cas d'egalite parfaite, le release echoue mais le refund aussi (car `vote_against >= vote_for` est vrai -> refund passe).

**Comportement actuel** : Conservateur - l'egalite mene au refund. Ce n'est pas forcement une faille, mais c'est un choix de design qui merite d'etre documente car il peut surprendre le createur.

---

#### 5. Pas de delai entre Funded et Release en mode auto (Severite : FAIBLE)

**Probleme** : En mode auto (validation_mode=1), des que l'objectif est atteint, le createur peut immediatement appeler `release` dans le meme bloc. Il n'y a aucun delai de reflexion ni de recours pour les contributeurs.

**Impact** : Les contributeurs n'ont aucun moyen de contester apres avoir contribue en mode auto. Certes, ils acceptent ce risque en contribuant a un pot en mode auto, mais un delai de securite (timelock) serait une bonne pratique.

**Correction possible** : Ajouter un champ `release_after` (ex: deadline du pot ou timestamp specifique) avant lequel le release est interdit.

---

#### 6. Absence de mecanisme d'urgence / pause (Severite : FAIBLE)

**Probleme** : Le contrat n'a aucun admin, aucun mecanisme de pause, aucun moyen d'intervenir en cas de bug decouvert apres deploiement.

**Avantage** : Decentralisation totale, pas de point de controle central.

**Inconvenient** : Si une faille est decouverte, impossible de geler le contrat pour proteger les fonds.

**Correction possible** : Ajouter un entrypoint `pause` reserve a un admin (multisig idealement) qui bloque toutes les operations sensibles. Cela introduit un compromis avec la decentralisation.

---

#### 7. Pas d'emission d'events (Severite : FAIBLE)

**Probleme** : Le contrat n'emet aucun event SmartPy. Il est donc difficile pour une application frontend ou un indexer de suivre l'activite du contrat en temps reel (nouvelles cagnottes, contributions, votes, releases...).

**Impact** : Pas de faille de securite, mais une limitation operationnelle importante pour un vrai deploiement.

**Correction possible** : Ajouter `sp.emit(...)` dans chaque entrypoint pour emettre des events indexes.

---

### Ameliorations fonctionnelles

#### 1. Deadline de vote

Ajouter un champ `vote_deadline` pour limiter la duree de la phase de vote. Apres ce delai, si le quorum n'est pas atteint, la cagnotte passe automatiquement en Failed. Cela resout le probleme des fonds bloques (faille #3).

#### 2. Vue (on-chain views)

Ajouter des `@sp.onchain_view` pour permettre a d'autres contrats ou au frontend de lire l'etat des cagnottes, les contributions, les votes, etc. sans avoir a parser le storage directement.

Exemples :
- `get_pot(pot_id)` -> retourne les infos de la cagnotte
- `get_contribution(pot_id, address)` -> retourne le montant contribue
- `has_voted(pot_id, address)` -> retourne si l'adresse a vote

#### 3. Contributions partielles au-dela de l'objectif

Actuellement, si une contribution depasse l'objectif, l'excedent reste dans le pot et sera envoye au createur. Par exemple, goal = 100 XTZ, deja 90 XTZ collectes, Alice envoie 50 XTZ -> total = 140 XTZ, tout va au createur.

**Amelioration** : Refuser la contribution si elle depasse l'objectif, ou rembourser automatiquement l'excedent.

#### 4. Limitation de titre/description

Aucune limite sur la taille du titre et de la description. Un utilisateur malveillant pourrait stocker de tres longues chaines et augmenter les couts de storage pour le contrat.

**Amelioration** : Ajouter des assertions sur `sp.len(title)` et `sp.len(description)`.

#### 5. Historique des contributions

La structure actuelle cumule les contributions sans historique. Impossible de savoir combien de fois un contributeur a contribue ni a quels moments.

**Amelioration** : Ajouter une big_map de listes ou emettre des events pour chaque contribution.

#### 6. Support multi-token (FA1.2 / FA2)

Le contrat ne supporte que le XTZ natif. Pour une plateforme de crowdfunding complete, le support de tokens FA1.2 ou FA2 serait un atout.

---

### Resume des protections deja en place

| Protection | Implementation |
|-----------|---------------|
| Anti-reentrancy | State mis a jour AVANT `sp.send` (Checks-Effects-Interactions) |
| Pull payment | Les contributeurs retirent eux-memes via `claim_refund` |
| Double vote | Verification `votes.contains(key) == False` |
| Double refund | Contribution mise a 0 avant envoi, verification `contribution > 0` |
| Controle d'acces | `release` et `cancel` reserves au createur, `vote` et `claim_refund` reserves aux contributeurs |
| Validation des montants | Verification `amount > 0` sur contribute, `amount == 0` sur les autres EP |
| Machine a etats | Assertions sur `pot.status` dans chaque EP pour empecher les transitions invalides |
