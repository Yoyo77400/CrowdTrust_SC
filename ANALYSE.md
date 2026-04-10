# CrowdTrust - Analyse du Smart Contract

## Partie 1 : Entrypoints, Views et Tests

### Entrypoints (9)

| # | Entrypoint | Params | Role | Acces | Pause |
|---|-----------|--------|------|-------|-------|
| 1 | `create_pot` | title, description, goal, deadline, validation_mode, vote_deadline | Cree une nouvelle cagnotte (statut Active). Modes : 0=Vote pondere, 1=Auto, 2=Vote democratique. Limites titre 1-50 car, description 0-500 car. En mode vote (0 ou 2), exige vote_deadline > deadline | Ouvert | Bloquee |
| 2 | `contribute` | pot_id (+ XTZ envoyes) | Contribue a une cagnotte active. Cumule les contributions par adresse. Passe en Funded quand l'objectif est atteint. Le createur peut contribuer (comme sur Leetchi) mais n'est pas compte dans contributor_count | Ouvert | Bloquee |
| 3 | `vote` | pot_id, approve | Vote pour ou contre la liberation des fonds. Mode 0 : poids = montant contribue. Mode 2 : poids = 1 voix. Le createur ne peut PAS voter. Le vote doit intervenir avant vote_deadline | Contributeurs (hors createur) | Bloquee |
| 4 | `release` | pot_id | Libere les fonds vers le createur. Mode vote (0/2) : apres vote_deadline, quorum 50% atteint, vote_for > vote_against. Mode 0 : quorum exclut la contribution du createur. Mode 2 : quorum base sur contributor_count. Mode auto (1) : timelock, uniquement apres pot.deadline | Createur uniquement | Bloquee |
| 5 | `claim_refund` | pot_id | Permet a un contributeur de retirer ses fonds si la cagnotte est Failed ou Cancelled. Pull-payment : le contributeur retire lui-meme | Contributeurs uniquement | Non bloquee |
| 6 | `cancel` | pot_id | Annule une cagnotte Active ou Funded. Les contributeurs peuvent ensuite reclamer leur remboursement | Createur uniquement | Non bloquee |
| 7 | `refund` | pot_id | Marque une cagnotte comme Failed. Active + deadline depassee, ou Funded + vote defavorable/egal, ou Funded + vote_deadline passee sans quorum atteint. Mode 0 : quorum exclut la contribution du createur. Mode 2 : quorum base sur contributor_count | Ouvert | Non bloquee |
| 8 | `set_pause` | paused (bool) | Active ou desactive la pause du contrat. La pause bloque les EP 1-4 mais laisse accessibles les operations de securite (5-7) | Admin uniquement | - |
| 9 | `transfer_admin` | new_admin (address) | Transfere le role admin a une nouvelle adresse | Admin uniquement | - |

### On-chain Views (2)

| # | Vue | Param | Retour | Role |
|---|-----|-------|--------|------|
| 1 | `get_pot_info` | pot_id (nat) | pot_type record | Retourne toutes les informations d'une cagnotte |
| 2 | `get_contribution` | (pot_id, address) pair | mutez | Retourne le montant contribue par une adresse a une cagnotte donnee |

### Events emis (9)

| Event | Entrypoint | Donnees |
|-------|-----------|---------|
| `PotCreated` | create_pot | creator, goal |
| `Contribution` | contribute | pot_id, contributor, amount |
| `VoteCast` | vote | pot_id, voter, approve |
| `FundsReleased` | release | pot_id, amount |
| `RefundClaimed` | claim_refund | pot_id, contributor, amount |
| `PotCancelled` | cancel | pot_id |
| `PotFailed` | refund | pot_id |
| `PauseChanged` | set_pause | paused |
| `AdminTransferred` | transfer_admin | new_admin |

### Machine a etats

```
Active (0) --[objectif atteint]--> Funded (1) --[release]--> Released (2)
    |                                  |
    |--[deadline depassee]-->    Failed (3) <--[vote defavorable/egal]--|
    |                                  ^         |
    |--[cancel]-->          Cancelled (4)        |--[vote_deadline passee
    |                            ^               |   + quorum non atteint]
    |                            |
    +--- Funded (1) --[cancel]---+
```

### Modes de validation

| Mode | Nom | Vote | Quorum | Release |
|------|-----|------|--------|---------|
| 0 | Vote pondere | Poids = montant contribue | 50% de (total_contributed - contribution createur) | Apres vote_deadline, quorum atteint, vote_for > vote_against |
| 1 | Auto | Pas de vote | - | Apres pot.deadline (timelock) |
| 2 | Vote democratique | Poids = 1 voix par contributeur | 50% de contributor_count | Apres vote_deadline, quorum atteint, vote_for > vote_against |

### Regle d'egalite (vote_for == vote_against)

En cas de vote egalitaire, le resultat est considere comme **DEFAVORABLE**. C'est un choix de design conservateur qui protege les contributeurs en cas d'ambiguite :

- `release` : **REFUSE** — la condition exige `vote_for > vote_against` (strictement superieur). Une egalite ne satisfait pas cette condition
- `refund` : **AUTORISE** — la condition verifie `vote_against >= vote_for` (superieur ou egal). Une egalite satisfait cette condition

Le createur ne peut donc pas obtenir les fonds si le vote est partage. Les contributeurs peuvent recuperer leur mise via `claim_refund` apres que `refund` a marque la cagnotte comme Failed.

### Mecanisme de pause

L'admin (defini au deploiement, transferable via `transfer_admin`) peut mettre le contrat en pause via `set_pause(True)`. La pause bloque les operations d'entree de fonds et de gouvernance :

| Operation | Bloquee par la pause | Raison |
|-----------|---------------------|--------|
| `create_pot` | Oui | Empeche la creation de nouvelles cagnottes |
| `contribute` | Oui | Empeche l'entree de nouveaux fonds |
| `vote` | Oui | Gele la gouvernance |
| `release` | Oui | Empeche la liberation de fonds |
| `claim_refund` | **Non** | Les contributeurs doivent pouvoir recuperer leurs fonds en urgence |
| `cancel` | **Non** | Le createur doit pouvoir annuler pour debloquer les remboursements |
| `refund` | **Non** | Marquer comme Failed doit rester possible pour la securite des fonds |

---

### Tests unitaires (8 suites, 66 cas)

#### 1. `test_create_pot` - 11 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Creation reussie avec parametres valides (mode vote pondere) | Happy path | Pot cree, status=0, creator correct, contributor_count=0, vote_deadline stockee |
| 2 | ID auto-incremente (mode auto) | Happy path | next_id == 2, validation_mode == 1 |
| 3 | Creation reussie en mode vote democratique | Happy path | validation_mode == 2, next_id == 3 |
| 4 | Echec si goal = 0 | Erreur | `GOAL_MUST_BE_POSITIVE` |
| 5 | Echec si deadline dans le passe | Erreur | `DEADLINE_MUST_BE_FUTURE` |
| 6 | Echec si validation_mode invalide (5) | Erreur | `INVALID_VALIDATION_MODE` |
| 7 | Echec si titre trop long (> 50 caracteres) | Erreur | `TITLE_TOO_LONG` |
| 8 | Echec si description trop longue (> 500 caracteres) | Erreur | `DESCRIPTION_TOO_LONG` |
| 9 | Echec si vote_deadline <= deadline en mode vote | Erreur | `VOTE_DEADLINE_MUST_BE_AFTER_DEADLINE` |
| 10 | Echec si titre vide | Erreur | `TITLE_EMPTY` |
| 11 | Echec si vote_deadline <= deadline en mode democratique | Erreur | `VOTE_DEADLINE_MUST_BE_AFTER_DEADLINE` |

#### 2. `test_contribute` - 10 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Contribution reussie avec montant > 0 | Happy path | total_contributed mis a jour, contributor_count == 1 |
| 2 | Contributions multiples du meme user (cumul, pas de double comptage) | Happy path | Montants cumules, contributor_count inchange |
| 3 | Verification total_contributed mis a jour | Happy path | Somme correcte, contributor_count == 2 |
| 4 | Passage au statut Funded si objectif atteint | Happy path | status == 1 |
| 5 | Createur peut contribuer mais n'est pas compte dans contributor_count | Happy path | contributor_count == 0 apres contribution createur, puis 1 apres alice |
| 6 | Echec si montant = 0 | Erreur | `CONTRIBUTION_MUST_BE_POSITIVE` |
| 7 | Echec si cagnotte inexistante | Erreur | `POT_NOT_FOUND` |
| 8 | Echec si deadline depassee | Erreur | `DEADLINE_PASSED` |
| 9 | Passage automatique au statut Funded (mode auto) | Happy path | status == 1 |
| 10 | Echec si cagnotte non active (Funded) | Erreur | `POT_NOT_ACTIVE` |

#### 3. `test_vote` - 10 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Vote favorable enregistre (pondere) | Happy path | vote_for incremente du montant contribue |
| 2 | Vote defavorable enregistre (pondere) | Happy path | vote_against incremente |
| 3 | Verification poids et cumul vote_for / vote_against | Happy path | Totaux corrects apres plusieurs votes |
| 4 | Vote democratique : poids = 1 voix (pas le montant) | Happy path | vote_for/vote_against incrementes de sp.mutez(1) par votant |
| 5 | Echec si pas contributeur | Erreur | `NOT_A_CONTRIBUTOR` |
| 6 | Echec si double vote | Erreur | `ALREADY_VOTED` |
| 7 | Echec si cagnotte pas en statut Funded | Erreur | `POT_NOT_FUNDED` |
| 8 | Echec si mode auto | Erreur | `POT_IS_AUTO_MODE` |
| 9 | Echec si le createur tente de voter (anti-manipulation) | Erreur | `CREATOR_CANNOT_VOTE` |
| 10 | Echec si periode de vote terminee (apres vote_deadline) | Erreur | `VOTE_PERIOD_ENDED` |

#### 4. `test_release` - 11 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Release reussi apres vote pondere favorable (apres vote_deadline) | Happy path | status == 2, fonds envoyes au createur |
| 2 | Release reussi en mode auto (apres deadline = timelock) | Happy path | status == 2 |
| 3 | Echec release auto avant deadline (timelock) | Erreur | `RELEASE_TOO_EARLY` |
| 4 | Echec si vote defavorable (apres vote_deadline) | Erreur | `VOTE_NOT_FAVORABLE` |
| 5 | Echec si non-createur | Erreur | `NOT_CREATOR` |
| 6 | Echec si pas en statut Funded | Erreur | `POT_NOT_FUNDED` |
| 7 | Echec si deja Released | Erreur | `POT_NOT_FUNDED` |
| 8 | Echec si periode de vote pas terminee | Erreur | `VOTE_PERIOD_NOT_ENDED` |
| 9 | Echec en cas de vote egalitaire (vote_for == vote_against) | Erreur | `VOTE_NOT_FAVORABLE` |
| 10 | Release reussi en mode democratique (apres vote_deadline) | Happy path | status == 2 |
| 11 | Release avec quorum ajuste (contribution createur exclue du quorum) | Happy path | status == 2, quorum base sur total hors createur |

#### 5. `test_claim_refund` - 6 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Remboursement correct en cas d'echec (Failed) | Happy path | Fonds rendus au contributeur |
| 2 | Remboursement correct en cas d'annulation (Cancelled) | Happy path | Fonds rendus |
| 3 | Echec si cagnotte active | Erreur | `REFUND_NOT_AVAILABLE` |
| 4 | Echec si cagnotte Released | Erreur | `REFUND_NOT_AVAILABLE` |
| 5 | Echec si pas contributeur | Erreur | `NOT_A_CONTRIBUTOR` |
| 6 | Echec si double remboursement | Erreur | `ALREADY_REFUNDED` |

#### 6. `test_cancel` - 4 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Annulation reussie par le createur | Happy path | status == 4 |
| 2 | Echec si non-createur | Erreur | `NOT_CREATOR` |
| 3 | Echec si fonds deja liberes (Released) | Erreur | `CANNOT_CANCEL` |
| 4 | Echec si deja annulee | Erreur | `CANNOT_CANCEL` |

#### 7. `test_refund` - 7 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Deadline depassee -> Failed (Active) | Happy path | status == 3 |
| 2 | Vote negatif pendant periode de vote -> Failed (Funded) | Happy path | status == 3 |
| 3 | Echec si deadline non depassee | Erreur | `DEADLINE_NOT_PASSED` |
| 4 | Echec si deja Failed | Erreur | `REFUND_NOT_APPLICABLE` |
| 5 | Vote_deadline passee + quorum non atteint -> fonds debloques | Happy path | status == 3 |
| 6 | Vote egalitaire -> refund autorise (vote_against >= vote_for) | Happy path | status == 3 |
| 7 | Refund en mode democratique (quorum non atteint apres vote_deadline) | Happy path | status == 3 |

#### 8. `test_admin` - 12 cas

| # | Description | Type | Resultat attendu |
|---|------------|------|-----------------|
| 1 | Admin peut activer la pause | Happy path | paused == True |
| 2 | Echec si non-admin tente de pauser | Erreur | `NOT_ADMIN` |
| 3 | create_pot bloque quand pause | Erreur | `CONTRACT_PAUSED` |
| 4 | contribute bloque quand pause | Erreur | `CONTRACT_PAUSED` |
| 5 | vote bloque quand pause | Erreur | `CONTRACT_PAUSED` |
| 6 | release bloque quand pause | Erreur | `CONTRACT_PAUSED` |
| 7 | cancel fonctionne meme en pause (operation de securite) | Happy path | status == 4 |
| 8 | claim_refund fonctionne meme en pause (operation de securite) | Happy path | Fonds rendus |
| 9 | Admin desactive la pause | Happy path | paused == False |
| 10 | Admin transfere le role a new_admin | Happy path | admin == new_admin.address |
| 11 | Ancien admin ne peut plus pauser | Erreur | `NOT_ADMIN` |
| 12 | Nouveau admin peut pauser | Happy path | paused == True |

### Scenarios End-to-End (8)

| # | Scenario | Flux |
|---|---------|------|
| 1 | `test_e2e_success` | Create -> Contribute (x3, objectif atteint) -> Vote OUI majoritaire -> Release (apres vote_deadline) |
| 2 | `test_e2e_deadline_failure` | Create -> Contributions insuffisantes -> Deadline passee -> Refund -> Claim refund |
| 3 | `test_e2e_cancel` | Create -> Contribute -> Cancel -> Claim refund |
| 4 | `test_e2e_negative_vote` | Create -> Contribute (x3, objectif atteint) -> Vote NON majoritaire -> Release echoue -> Refund -> Claim refund (x3) |
| 5 | `test_e2e_quorum_not_reached` | Create -> Contribute (x3, objectif atteint) -> Personne ne vote -> Vote_deadline passee -> Release echoue (quorum) -> Refund -> Claim refund (x3) |
| 6 | `test_e2e_tied_vote` | Create -> Contribute (x2 egaux) -> Vote 50/50 -> Release echoue (egalite) -> Refund -> Claim refund (x2) |
| 7 | `test_e2e_democratic_vote` | Create (mode 2) -> Contribute (alice 80%, bob 15%, charlie 5%) -> Alice OUI, Bob+Charlie NON -> Release echoue (1 voix < 2 voix malgre 80% des fonds) -> Refund -> Claim refund (x3) |
| 8 | `test_e2e_quorum_not_reached` | Create -> Contribute (x3) -> Personne ne vote -> vote_deadline passee -> Release echoue -> Refund -> Claim refund (x3) |

---

## Partie 2 : Ameliorations possibles et Failles potentielles restantes

### Failles corrigees

Toutes les failles identifiees dans les analyses precedentes ont ete corrigees :

| Faille | Severite | Correction appliquee |
|--------|---------|---------------------|
| Createur pouvait voter et manipuler le resultat | MOYENNE | `assert sp.sender != pot.creator, "CREATOR_CANNOT_VOTE"` — le createur peut contribuer (comme Leetchi) mais pas voter |
| Vote ploutocratique (1 gros contributeur domine) | MOYENNE | Nouveau mode `validation_mode=2` (vote democratique, 1 personne = 1 voix). Le createur choisit le mode a la creation |
| Quorum biaise par la contribution du createur | FAIBLE | En mode 0, le quorum exclut la contribution du createur : `quorum_base = total_contributed - creator_contrib`. En mode 2, le quorum est base sur `contributor_count` (hors createur) |
| Fonds bloques si quorum jamais atteint | MOYENNE | `vote_deadline` obligatoire en mode vote. Apres cette date, `refund()` debloque les fonds si quorum non atteint |
| Pas de delai entre Funded et Release en mode auto | FAIBLE | Timelock : `assert sp.now > pot.deadline, "RELEASE_TOO_EARLY"` en mode auto |
| Absence de mecanisme d'urgence / pause | FAIBLE | Admin defini au deploiement. `set_pause(True)` bloque create_pot, contribute, vote, release. Les operations de securite (claim_refund, cancel, refund) restent accessibles |
| Pas d'events pour le suivi off-chain | FAIBLE | `sp.emit(...)` sur chaque entrypoint (9 types d'events) |
| Pas de vues on-chain | FAIBLE | 2 `@sp.onchain_view` : `get_pot_info` et `get_contribution` |
| Pas de limite titre/description (abus storage) | FAIBLE | `sp.len(title)` entre 1 et 50, `sp.len(description)` max 500 |
| Egalite de vote non documentee | FAIBLE | Documentee dans le code, testee (2 tests unitaires + 1 E2E dedie) |

---

### Ameliorations fonctionnelles possibles

#### 1. Support multi-token (FA1.2 / FA2)

Le contrat ne supporte que le XTZ natif. Pour une plateforme de crowdfunding complete, le support de tokens FA1.2 ou FA2 (stablecoins, tokens de gouvernance) serait un atout significatif.

#### 2. Historique detaille des contributions

La structure actuelle cumule les contributions sans historique temporel. Impossible de savoir combien de fois un contributeur a contribue ni a quels moments. Les events `Contribution` emis permettent un suivi off-chain via un indexer, mais pas de lecture on-chain de l'historique.

#### 3. Delegation de vote

Permettre a un contributeur de deleguer son vote a un autre contributeur, utile pour les cagnottes avec beaucoup de petits contributeurs qui ne souhaitent pas voter activement.

#### 4. Vote quadratique

Ajouter un `validation_mode=3` pour un vote quadratique ou le poids = racine carree du montant contribue. Compromis entre le vote pondere (qui favorise les gros contributeurs) et le vote democratique (qui ignore completement les montants).

---

### Resume des protections en place

| Protection | Implementation |
|-----------|---------------|
| Anti-reentrancy | State mis a jour AVANT `sp.send` (Checks-Effects-Interactions) |
| Pull payment | Les contributeurs retirent eux-memes via `claim_refund` |
| Double vote | Verification `votes.contains(key) == False` |
| Double refund | Contribution mise a 0 avant envoi, verification `contribution > 0` |
| Controle d'acces | `release` et `cancel` reserves au createur ; `vote` reserve aux contributeurs hors createur ; `claim_refund` reserve aux contributeurs ; `set_pause` et `transfer_admin` reserves a l'admin |
| Anti-manipulation createur | Le createur peut contribuer mais ne peut PAS voter (`CREATOR_CANNOT_VOTE`). Sa contribution est exclue du calcul du quorum |
| Anti-ploutocratique | Mode democratique (validation_mode=2) disponible : 1 personne = 1 voix, quel que soit le montant |
| Deblocage des fonds | `vote_deadline` empechant les fonds bloques indefiniment si quorum non atteint |
| Periode de vote controllee | Vote possible uniquement avant `vote_deadline` ; release possible uniquement apres `vote_deadline` |
| Timelock mode auto | Release en mode auto uniquement apres `pot.deadline`, laissant un delai aux contributeurs |
| Mecanisme de pause | Admin peut geler les operations sensibles (create, contribute, vote, release) tout en preservant les operations de securite (claim_refund, cancel, refund) |
| Validation des montants | `amount > 0` sur contribute, `amount == 0` sur les autres EP |
| Validation des inputs | Titre : 1-50 car, description : 0-500 car, goal > 0, deadline > now, vote_deadline > deadline (mode 0/2), validation_mode <= 2 |
| Machine a etats | Assertions sur `pot.status` dans chaque EP pour empecher les transitions invalides |
| Tracabilite off-chain | 9 types d'events emis via `sp.emit` pour le suivi par indexer/frontend |
| Lisibilite on-chain | 2 vues `@sp.onchain_view` pour lire l'etat sans parser le storage |
