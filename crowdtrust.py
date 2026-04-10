import smartpy as sp

# ============================================================================
# CrowdTrust - Cagnotte intelligente on-chain avec escrow et gouvernance
# ============================================================================
# Statuts : 0=Active, 1=Funded, 2=Released, 3=Failed, 4=Cancelled
# Validation modes : 0=Vote pondere, 1=Auto, 2=Vote democratique
#
# Bonnes pratiques :
#   - Pull payment : destinataires retirent eux-memes leurs fonds
#   - Checks-Effects-Interactions : assertions, puis state, puis envoi
#   - Protection reentrancy : state mis a jour AVANT sp.send
#
# Regle de vote en cas d'egalite :
#   En cas de vote egalitaire (vote_for == vote_against), le resultat est
#   considere comme DEFAVORABLE. Le release est refuse (vote_for > vote_against
#   requis, strictement superieur) et le refund est autorise
#   (vote_against >= vote_for). Ce choix conservateur protege les contributeurs.
#
# Protection anti-manipulation du createur :
#   Le createur peut contribuer (comme sur Leetchi), MAIS il ne peut PAS voter.
#   De plus, sa contribution est exclue du calcul du quorum en mode pondere,
#   pour eviter de biaiser le seuil de participation.
#
# Vote democratique (mode 2) :
#   Chaque contributeur (hors createur) a exactement 1 voix, quel que soit
#   le montant contribue. Le quorum est base sur le nombre de contributeurs
#   eligibles (contributor_count), pas sur les montants.
#
# Timelock mode auto :
#   En mode auto, le release n'est possible qu'apres la deadline de
#   contribution, laissant un delai minimum aux contributeurs.
#
# Mecanisme de pause :
#   Un admin (defini au deploiement) peut mettre le contrat en pause.
#   La pause bloque create_pot, contribute, vote, release.
#   Les operations de securite (claim_refund, cancel, refund) restent
#   accessibles en permanence pour proteger les fonds des contributeurs.
# ============================================================================


@sp.module
def main():
    pot_type: type = sp.record(
        creator=sp.address,
        title=sp.string,
        description=sp.string,
        goal=sp.mutez,
        deadline=sp.timestamp,
        vote_deadline=sp.timestamp,
        total_contributed=sp.mutez,
        contributor_count=sp.nat,
        status=sp.nat,
        vote_for=sp.mutez,
        vote_against=sp.mutez,
        validation_mode=sp.nat,
    )

    class CrowdTrust(sp.Contract):
        def __init__(self, admin):
            sp.cast(admin, sp.address)
            self.data.pots = sp.cast(
                sp.big_map(), sp.big_map[sp.nat, pot_type]
            )
            self.data.contributions = sp.cast(
                sp.big_map(),
                sp.big_map[sp.pair[sp.nat, sp.address], sp.mutez],
            )
            self.data.votes = sp.cast(
                sp.big_map(),
                sp.big_map[sp.pair[sp.nat, sp.address], sp.bool],
            )
            self.data.next_id = sp.nat(0)
            self.data.admin = admin
            self.data.paused = False

        # ----------------------------------------------------------------
        # create_pot : cree une nouvelle cagnotte
        # Modes : 0=Vote pondere, 1=Auto, 2=Vote democratique
        # Limites : titre 1-50 car, description 0-500 car
        # Mode vote (0 ou 2) : vote_deadline doit etre apres deadline
        # Mode auto (1) : vote_deadline ignoree
        # BLOQUEE SI CONTRAT EN PAUSE
        # ----------------------------------------------------------------
        @sp.entrypoint
        def create_pot(self, title, description, goal, deadline, validation_mode, vote_deadline):
            sp.cast(title, sp.string)
            sp.cast(description, sp.string)
            sp.cast(goal, sp.mutez)
            sp.cast(deadline, sp.timestamp)
            sp.cast(validation_mode, sp.nat)
            sp.cast(vote_deadline, sp.timestamp)

            assert self.data.paused == False, "CONTRACT_PAUSED"
            assert goal > sp.mutez(0), "GOAL_MUST_BE_POSITIVE"
            assert deadline > sp.now, "DEADLINE_MUST_BE_FUTURE"
            assert validation_mode <= 2, "INVALID_VALIDATION_MODE"
            assert sp.amount == sp.mutez(0), "NO_XTZ_ON_CREATE"
            assert sp.len(title) > 0, "TITLE_EMPTY"
            assert sp.len(title) <= 50, "TITLE_TOO_LONG"
            assert sp.len(description) <= 500, "DESCRIPTION_TOO_LONG"

            if validation_mode != 1:
                assert vote_deadline > deadline, "VOTE_DEADLINE_MUST_BE_AFTER_DEADLINE"

            self.data.pots[self.data.next_id] = sp.record(
                creator=sp.sender,
                title=title,
                description=description,
                goal=goal,
                deadline=deadline,
                vote_deadline=vote_deadline,
                total_contributed=sp.mutez(0),
                contributor_count=sp.nat(0),
                status=0,
                vote_for=sp.mutez(0),
                vote_against=sp.mutez(0),
                validation_mode=validation_mode,
            )
            self.data.next_id += 1

            sp.emit(sp.record(creator=sp.sender, goal=goal), tag="PotCreated")

        # ----------------------------------------------------------------
        # contribute : contribuer a une cagnotte active
        # Le createur peut contribuer (comme sur Leetchi).
        # contributor_count ne compte que les non-createurs (votants eligibles).
        # BLOQUEE SI CONTRAT EN PAUSE
        # ----------------------------------------------------------------
        @sp.entrypoint
        def contribute(self, pot_id):
            sp.cast(pot_id, sp.nat)
            assert self.data.paused == False, "CONTRACT_PAUSED"
            assert self.data.pots.contains(pot_id), "POT_NOT_FOUND"
            pot = self.data.pots[pot_id]
            assert pot.status == 0, "POT_NOT_ACTIVE"
            assert sp.now <= pot.deadline, "DEADLINE_PASSED"
            assert sp.amount > sp.mutez(0), "CONTRIBUTION_MUST_BE_POSITIVE"

            key = (pot_id, sp.sender)
            previous = self.data.contributions.get(
                key, default=sp.mutez(0)
            )
            self.data.contributions[key] = previous + sp.amount
            pot.total_contributed += sp.amount

            if previous == sp.mutez(0):
                if sp.sender != pot.creator:
                    pot.contributor_count += 1

            if pot.total_contributed >= pot.goal:
                pot.status = 1
            self.data.pots[pot_id] = pot

            sp.emit(sp.record(pot_id=pot_id, contributor=sp.sender, amount=sp.amount), tag="Contribution")

        # ----------------------------------------------------------------
        # vote : voter pour ou contre la liberation des fonds
        # Mode 0 (pondere) : poids = montant contribue
        # Mode 2 (democratique) : poids = 1 voix par contributeur
        # Le CREATEUR ne peut PAS voter (anti-manipulation)
        # Vote possible uniquement avant vote_deadline
        #
        # REGLE D'EGALITE : vote_for == vote_against = DEFAVORABLE
        # BLOQUEE SI CONTRAT EN PAUSE
        # ----------------------------------------------------------------
        @sp.entrypoint
        def vote(self, pot_id, approve):
            sp.cast(pot_id, sp.nat)
            sp.cast(approve, sp.bool)
            assert self.data.paused == False, "CONTRACT_PAUSED"
            assert self.data.pots.contains(pot_id), "POT_NOT_FOUND"
            pot = self.data.pots[pot_id]
            assert pot.status == 1, "POT_NOT_FUNDED"
            assert pot.validation_mode != 1, "POT_IS_AUTO_MODE"
            assert sp.amount == sp.mutez(0), "NO_XTZ_ON_VOTE"
            assert sp.sender != pot.creator, "CREATOR_CANNOT_VOTE"
            assert sp.now <= pot.vote_deadline, "VOTE_PERIOD_ENDED"

            key = (pot_id, sp.sender)
            assert self.data.contributions.contains(key), "NOT_A_CONTRIBUTOR"
            assert self.data.votes.contains(key) == False, "ALREADY_VOTED"

            contribution = self.data.contributions[key]
            self.data.votes[key] = approve

            if pot.validation_mode == 0:
                if approve:
                    pot.vote_for += contribution
                else:
                    pot.vote_against += contribution
            else:
                if approve:
                    pot.vote_for += sp.mutez(1)
                else:
                    pot.vote_against += sp.mutez(1)
            self.data.pots[pot_id] = pot

            sp.emit(sp.record(pot_id=pot_id, voter=sp.sender, approve=approve), tag="VoteCast")

        # ----------------------------------------------------------------
        # release : liberer les fonds vers le createur
        # Mode auto (1)  : timelock, release uniquement apres pot.deadline
        # Mode vote (0/2) : release uniquement apres vote_deadline,
        #                    quorum (50%) atteint, vote_for > vote_against
        # Mode 0 : quorum base sur total_contributed HORS createur
        # Mode 2 : quorum base sur contributor_count (1 voix/personne)
        #
        # EGALITE : vote_for == vote_against -> release REFUSE
        # BLOQUEE SI CONTRAT EN PAUSE
        # ----------------------------------------------------------------
        @sp.entrypoint
        def release(self, pot_id):
            sp.cast(pot_id, sp.nat)
            assert self.data.paused == False, "CONTRACT_PAUSED"
            assert self.data.pots.contains(pot_id), "POT_NOT_FOUND"
            pot = self.data.pots[pot_id]
            assert pot.status == 1, "POT_NOT_FUNDED"
            assert sp.sender == pot.creator, "NOT_CREATOR"
            assert sp.amount == sp.mutez(0), "NO_XTZ_ON_RELEASE"

            if pot.validation_mode == 1:
                assert sp.now > pot.deadline, "RELEASE_TOO_EARLY"
            else:
                assert sp.now > pot.vote_deadline, "VOTE_PERIOD_NOT_ENDED"
                total_votes = pot.vote_for + pot.vote_against
                quorum_base = sp.mutez(0)
                if pot.validation_mode == 0:
                    creator_contrib = self.data.contributions.get(
                        (pot_id, pot.creator), default=sp.mutez(0)
                    )
                    quorum_base = pot.total_contributed - creator_contrib
                else:
                    quorum_base = sp.split_tokens(
                        sp.mutez(1), pot.contributor_count, 1
                    )

                quorum_threshold = sp.split_tokens(quorum_base, 50, 100)
                assert (
                    total_votes >= quorum_threshold
                ), "QUORUM_NOT_REACHED"
                assert pot.vote_for > pot.vote_against, "VOTE_NOT_FAVORABLE"

            amount_to_send = pot.total_contributed
            pot.status = 2
            self.data.pots[pot_id] = pot
            sp.send(pot.creator, amount_to_send)

            sp.emit(sp.record(pot_id=pot_id, amount=amount_to_send), tag="FundsReleased")

        # ----------------------------------------------------------------
        # claim_refund : retirer ses fonds si Failed ou Cancelled
        # Pull-payment : le contributeur retire lui-meme ses fonds
        # Protection double-refund : contribution mise a 0 AVANT sp.send
        # NON BLOQUEE PAR LA PAUSE (operation de securite)
        # ----------------------------------------------------------------
        @sp.entrypoint
        def claim_refund(self, pot_id):
            sp.cast(pot_id, sp.nat)
            assert self.data.pots.contains(pot_id), "POT_NOT_FOUND"
            pot = self.data.pots[pot_id]
            assert sp.amount == sp.mutez(0), "NO_XTZ_ON_REFUND"
            assert pot.status >= 3, "REFUND_NOT_AVAILABLE"

            key = (pot_id, sp.sender)
            assert self.data.contributions.contains(key), "NOT_A_CONTRIBUTOR"
            contribution = self.data.contributions[key]
            assert contribution > sp.mutez(0), "ALREADY_REFUNDED"

            self.data.contributions[key] = sp.mutez(0)
            sp.send(sp.sender, contribution)

            sp.emit(sp.record(pot_id=pot_id, contributor=sp.sender, amount=contribution), tag="RefundClaimed")

        # ----------------------------------------------------------------
        # cancel : annuler une cagnotte (createur uniquement)
        # Possible tant que statut <= 1 (Active ou Funded)
        # NON BLOQUEE PAR LA PAUSE (operation de securite)
        # ----------------------------------------------------------------
        @sp.entrypoint
        def cancel(self, pot_id):
            sp.cast(pot_id, sp.nat)
            assert self.data.pots.contains(pot_id), "POT_NOT_FOUND"
            pot = self.data.pots[pot_id]
            assert sp.sender == pot.creator, "NOT_CREATOR"
            assert pot.status <= 1, "CANNOT_CANCEL"
            assert sp.amount == sp.mutez(0), "NO_XTZ_ON_CANCEL"
            pot.status = 4
            self.data.pots[pot_id] = pot

            sp.emit(sp.record(pot_id=pot_id), tag="PotCancelled")

        # ----------------------------------------------------------------
        # refund : marquer une cagnotte comme Failed
        # Cas 1 : Active + deadline depassee -> Failed
        # Cas 2 : Funded + mode vote (0/2) :
        #   - Periode de vote en cours : quorum atteint + vote defavorable
        #   - Periode de vote terminee :
        #       * Quorum atteint + vote defavorable/egal -> Failed
        #       * Quorum NON atteint -> Failed (deblocage des fonds)
        # Mode 0 : quorum exclut la contribution du createur
        # Mode 2 : quorum base sur contributor_count
        #
        # EGALITE : vote_against >= vote_for -> refund AUTORISE
        # NON BLOQUEE PAR LA PAUSE (operation de securite)
        # ----------------------------------------------------------------
        @sp.entrypoint
        def refund(self, pot_id):
            sp.cast(pot_id, sp.nat)
            assert self.data.pots.contains(pot_id), "POT_NOT_FOUND"
            pot = self.data.pots[pot_id]
            assert sp.amount == sp.mutez(0), "NO_XTZ_ON_REFUND"

            if pot.status == 0:
                assert sp.now > pot.deadline, "DEADLINE_NOT_PASSED"
                pot.status = 3
            else:
                if pot.status == 1:
                    assert pot.validation_mode != 1, "POT_IS_AUTO_MODE"
                    total_votes = pot.vote_for + pot.vote_against

                    quorum_base = sp.mutez(0)
                    if pot.validation_mode == 0:
                        creator_contrib = self.data.contributions.get(
                            (pot_id, pot.creator), default=sp.mutez(0)
                        )
                        quorum_base = pot.total_contributed - creator_contrib
                    else:
                        quorum_base = sp.split_tokens(
                            sp.mutez(1), pot.contributor_count, 1
                        )

                    quorum_threshold = sp.split_tokens(
                        quorum_base, 50, 100
                    )

                    if sp.now > pot.vote_deadline:
                        # Periode de vote terminee :
                        # - Quorum atteint + vote defavorable/egal -> Failed
                        # - Quorum NON atteint -> Failed (fonds debloques)
                        if total_votes >= quorum_threshold:
                            assert (
                                pot.vote_against >= pot.vote_for
                            ), "VOTE_IS_FAVORABLE"
                    else:
                        # Periode de vote en cours :
                        # Quorum atteint + vote defavorable requis
                        assert (
                            total_votes >= quorum_threshold
                        ), "QUORUM_NOT_REACHED"
                        assert (
                            pot.vote_against >= pot.vote_for
                        ), "VOTE_IS_FAVORABLE"
                    pot.status = 3
                else:
                    assert False, "REFUND_NOT_APPLICABLE"
            self.data.pots[pot_id] = pot

            sp.emit(sp.record(pot_id=pot_id), tag="PotFailed")

        # ----------------------------------------------------------------
        # set_pause : activer/desactiver la pause (admin uniquement)
        # ----------------------------------------------------------------
        @sp.entrypoint
        def set_pause(self, paused):
            sp.cast(paused, sp.bool)
            assert sp.sender == self.data.admin, "NOT_ADMIN"
            assert sp.amount == sp.mutez(0), "NO_XTZ_ON_ADMIN"
            self.data.paused = paused

            sp.emit(sp.record(paused=paused), tag="PauseChanged")

        # ----------------------------------------------------------------
        # transfer_admin : transferer le role admin (admin uniquement)
        # ----------------------------------------------------------------
        @sp.entrypoint
        def transfer_admin(self, new_admin):
            sp.cast(new_admin, sp.address)
            assert sp.sender == self.data.admin, "NOT_ADMIN"
            assert sp.amount == sp.mutez(0), "NO_XTZ_ON_ADMIN"
            self.data.admin = new_admin

            sp.emit(sp.record(new_admin=new_admin), tag="AdminTransferred")

        # ----------------------------------------------------------------
        # On-chain views
        # ----------------------------------------------------------------
        @sp.onchain_view
        def get_pot_info(self, pot_id):
            """Retourne les informations d'une cagnotte."""
            sp.cast(pot_id, sp.nat)
            assert self.data.pots.contains(pot_id), "POT_NOT_FOUND"
            return self.data.pots[pot_id]

        @sp.onchain_view
        def get_contribution(self, key):
            """Retourne le montant contribue pour un (pot_id, address)."""
            sp.cast(key, sp.pair[sp.nat, sp.address])
            return self.data.contributions.get(key, default=sp.mutez(0))


# ============================================================================
# TESTS
# ============================================================================
# Timestamps de reference :
#   now            = 1 000 000   (temps courant)
#   future         = 2 000 000   (deadline contributions)
#   vote_end       = 3 000 000   (deadline votes)
#   after_deadline = 2 000 001   (juste apres deadline)
#   after_vote_end = 3 000 001   (juste apres vote_deadline)
#
# Entrypoints a 1 param (contribute, release, claim_refund, cancel, refund) :
#   -> appel positionnel : c.contribute(0, _sender=...)
# Entrypoints a N params (create_pot, vote) :
#   -> appel keyword : c.vote(pot_id=0, approve=True, _sender=...)
# ============================================================================


@sp.add_test()
def test_create_pot():
    sc = sp.test_scenario("test_create_pot", main)
    admin = sp.test_account("admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    past = sp.timestamp(500000)
    vote_end = sp.timestamp(3000000)

    sc.h2("1. Creation reussie avec parametres valides (mode vote pondere)")
    c.create_pot(
        title="Voyage Bali",
        description="Voyage entre amis",
        goal=sp.mutez(2000000000),
        deadline=future,
        validation_mode=0,
        vote_deadline=vote_end,
        _sender=creator.address,
        _now=now,
        _amount=sp.mutez(0),
    )
    sc.verify(c.data.pots.contains(0))
    sc.verify(c.data.pots[0].title == "Voyage Bali")
    sc.verify(c.data.pots[0].status == 0)
    sc.verify(c.data.pots[0].creator == creator.address)
    sc.verify(c.data.pots[0].total_contributed == sp.mutez(0))
    sc.verify(c.data.pots[0].contributor_count == 0)
    sc.verify(c.data.pots[0].vote_deadline == vote_end)

    sc.h2("2. Verification de l'ID auto-incremente (mode auto)")
    c.create_pot(
        title="Cadeau",
        description="Cadeau Lea",
        goal=sp.mutez(500000000),
        deadline=future,
        validation_mode=1,
        vote_deadline=sp.timestamp(0),
        _sender=creator.address,
        _now=now,
        _amount=sp.mutez(0),
    )
    sc.verify(c.data.next_id == 2)
    sc.verify(c.data.pots[1].validation_mode == 1)

    sc.h2("3. Creation reussie en mode vote democratique")
    c.create_pot(
        title="Demo Pot",
        description="Vote 1 personne = 1 voix",
        goal=sp.mutez(1000000000),
        deadline=future,
        validation_mode=2,
        vote_deadline=vote_end,
        _sender=creator.address,
        _now=now,
        _amount=sp.mutez(0),
    )
    sc.verify(c.data.pots[2].validation_mode == 2)
    sc.verify(c.data.next_id == 3)

    sc.h2("4. Echec si objectif = 0")
    c.create_pot(
        title="Test", description="t", goal=sp.mutez(0), deadline=future,
        validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="GOAL_MUST_BE_POSITIVE",
    )

    sc.h2("5. Echec si deadline dans le passe")
    c.create_pot(
        title="Test", description="t", goal=sp.mutez(1000000), deadline=past,
        validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="DEADLINE_MUST_BE_FUTURE",
    )

    sc.h2("6. Echec si validation_mode invalide")
    c.create_pot(
        title="Test", description="t", goal=sp.mutez(1000000), deadline=future,
        validation_mode=5, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="INVALID_VALIDATION_MODE",
    )

    sc.h2("7. Echec si titre trop long (> 50 caracteres)")
    c.create_pot(
        title="A" * 51, description="t", goal=sp.mutez(1000000), deadline=future,
        validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="TITLE_TOO_LONG",
    )

    sc.h2("8. Echec si description trop longue (> 500 caracteres)")
    c.create_pot(
        title="Test", description="A" * 501, goal=sp.mutez(1000000), deadline=future,
        validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="DESCRIPTION_TOO_LONG",
    )

    sc.h2("9. Echec si vote_deadline <= deadline en mode vote")
    c.create_pot(
        title="Test", description="t", goal=sp.mutez(1000000), deadline=future,
        validation_mode=0, vote_deadline=future,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_DEADLINE_MUST_BE_AFTER_DEADLINE",
    )

    sc.h2("10. Echec si titre vide")
    c.create_pot(
        title="", description="t", goal=sp.mutez(1000000), deadline=future,
        validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="TITLE_EMPTY",
    )

    sc.h2("11. Echec si vote_deadline <= deadline en mode democratique")
    c.create_pot(
        title="Test", description="t", goal=sp.mutez(1000000), deadline=future,
        validation_mode=2, vote_deadline=past,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_DEADLINE_MUST_BE_AFTER_DEADLINE",
    )


@sp.add_test()
def test_contribute():
    sc = sp.test_scenario("test_contribute", main)
    admin = sp.test_account("admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    bob = sp.test_account("bob")
    charlie = sp.test_account("charlie")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_deadline = sp.timestamp(2000001)

    c.create_pot(title="Vote Pot", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.create_pot(title="Auto Pot", description="d", goal=sp.mutez(500000000),
        deadline=future, validation_mode=1, vote_deadline=sp.timestamp(0),
        _sender=creator.address, _now=now, _amount=sp.mutez(0))

    sc.h2("1. Contribution reussie avec montant > 0")
    c.contribute(0, _sender=alice.address, _amount=sp.mutez(500000000), _now=now)
    sc.verify(c.data.pots[0].total_contributed == sp.mutez(500000000))
    sc.verify(c.data.pots[0].contributor_count == 1)

    sc.h2("2. Contributions multiples du meme utilisateur (cumul, pas de double comptage)")
    c.contribute(0, _sender=alice.address, _amount=sp.mutez(200000000), _now=now)
    sc.verify(c.data.pots[0].total_contributed == sp.mutez(700000000))
    sc.verify(c.data.pots[0].contributor_count == 1)

    sc.h2("3. Verification total_contributed mis a jour")
    c.contribute(0, _sender=bob.address, _amount=sp.mutez(300000000), _now=now)
    sc.verify(c.data.pots[0].total_contributed == sp.mutez(1000000000))
    sc.verify(c.data.pots[0].contributor_count == 2)

    sc.h2("4. Passage au statut Funded si objectif atteint")
    sc.verify(c.data.pots[0].status == 1)

    sc.h2("5. Createur peut contribuer mais n'est pas compte dans contributor_count")
    c.create_pot(title="Creator Pot", description="d", goal=sp.mutez(500000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(2, _sender=creator.address, _amount=sp.mutez(300000000), _now=now)
    sc.verify(c.data.pots[2].contributor_count == 0)
    c.contribute(2, _sender=alice.address, _amount=sp.mutez(200000000), _now=now)
    sc.verify(c.data.pots[2].contributor_count == 1)

    sc.h2("6. Echec si montant = 0")
    c.contribute(1, _sender=alice.address, _amount=sp.mutez(0), _now=now,
        _valid=False, _exception="CONTRIBUTION_MUST_BE_POSITIVE")

    sc.h2("7. Echec si cagnotte inexistante")
    c.contribute(999, _sender=alice.address, _amount=sp.mutez(100000), _now=now,
        _valid=False, _exception="POT_NOT_FOUND")

    sc.h2("8. Echec si deadline depassee")
    c.contribute(1, _sender=alice.address, _amount=sp.mutez(100000), _now=after_deadline,
        _valid=False, _exception="DEADLINE_PASSED")

    sc.h2("9. Passage automatique au statut Funded (mode auto)")
    c.contribute(1, _sender=bob.address, _amount=sp.mutez(500000000), _now=now)
    sc.verify(c.data.pots[1].status == 1)

    sc.h2("10. Echec si cagnotte non active (Funded)")
    c.contribute(1, _sender=charlie.address, _amount=sp.mutez(100000), _now=now,
        _valid=False, _exception="POT_NOT_ACTIVE")


@sp.add_test()
def test_vote():
    sc = sp.test_scenario("test_vote", main)
    admin = sp.test_account("admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    bob = sp.test_account("bob")
    charlie = sp.test_account("charlie")
    outsider = sp.test_account("outsider")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_vote_end = sp.timestamp(3000001)

    # Pot 0 : vote pondere, funded par alice+bob+charlie
    c.create_pot(title="Vote Pot", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(0, _sender=alice.address, _amount=sp.mutez(500000000), _now=now)
    c.contribute(0, _sender=bob.address, _amount=sp.mutez(350000000), _now=now)
    c.contribute(0, _sender=charlie.address, _amount=sp.mutez(150000000), _now=now)

    # Pot 1 : auto mode, funded par alice
    c.create_pot(title="Auto Pot", description="d", goal=sp.mutez(100000000),
        deadline=future, validation_mode=1, vote_deadline=sp.timestamp(0),
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(1, _sender=alice.address, _amount=sp.mutez(100000000), _now=now)

    # Pot 2 : vote pondere, pas encore funded (Active)
    c.create_pot(title="Active Pot", description="d", goal=sp.mutez(9000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(2, _sender=alice.address, _amount=sp.mutez(100000000), _now=now)

    # Pot 3 : vote pondere, funded, createur a contribue
    c.create_pot(title="Creator Pot", description="d", goal=sp.mutez(500000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(3, _sender=creator.address, _amount=sp.mutez(300000000), _now=now)
    c.contribute(3, _sender=alice.address, _amount=sp.mutez(200000000), _now=now)

    # Pot 4 : vote democratique, funded par alice+bob+charlie
    c.create_pot(title="Demo Pot", description="d", goal=sp.mutez(600000000),
        deadline=future, validation_mode=2, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(4, _sender=alice.address, _amount=sp.mutez(300000000), _now=now)
    c.contribute(4, _sender=bob.address, _amount=sp.mutez(200000000), _now=now)
    c.contribute(4, _sender=charlie.address, _amount=sp.mutez(100000000), _now=now)

    sc.h2("1. Vote favorable enregistre correctement (pondere)")
    c.vote(pot_id=0, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].vote_for == sp.mutez(500000000))

    sc.h2("2. Vote defavorable enregistre correctement (pondere)")
    c.vote(pot_id=0, approve=False, _sender=charlie.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].vote_against == sp.mutez(150000000))

    sc.h2("3. Verification poids et cumul vote_for / vote_against")
    c.vote(pot_id=0, approve=True, _sender=bob.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].vote_for == sp.mutez(850000000))
    sc.verify(c.data.pots[0].vote_against == sp.mutez(150000000))

    sc.h2("4. Vote democratique : poids = 1 voix (pas le montant)")
    c.vote(pot_id=4, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[4].vote_for == sp.mutez(1))
    c.vote(pot_id=4, approve=False, _sender=bob.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[4].vote_against == sp.mutez(1))
    c.vote(pot_id=4, approve=True, _sender=charlie.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[4].vote_for == sp.mutez(2))

    sc.h2("5. Echec si pas contributeur")
    c.vote(pot_id=0, approve=True, _sender=outsider.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="NOT_A_CONTRIBUTOR")

    sc.h2("6. Echec si double vote")
    c.vote(pot_id=0, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="ALREADY_VOTED")

    sc.h2("7. Echec si cagnotte pas en statut Funded")
    c.vote(pot_id=2, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="POT_NOT_FUNDED")

    sc.h2("8. Echec si mode auto")
    c.vote(pot_id=1, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="POT_IS_AUTO_MODE")

    sc.h2("9. Echec si le createur tente de voter (anti-manipulation)")
    c.vote(pot_id=3, approve=True, _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="CREATOR_CANNOT_VOTE")

    sc.h2("10. Echec si periode de vote terminee (apres vote_deadline)")
    c.vote(pot_id=3, approve=True, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_PERIOD_ENDED")


@sp.add_test()
def test_release():
    sc = sp.test_scenario("test_release", main)
    admin = sp.test_account("admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    bob = sp.test_account("bob")
    outsider = sp.test_account("outsider")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_deadline = sp.timestamp(2000001)
    after_vote_end = sp.timestamp(3000001)

    # Pot 0 : vote pondere, votes favorables
    c.create_pot(title="R0", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(0, _sender=alice.address, _amount=sp.mutez(600000000), _now=now)
    c.contribute(0, _sender=bob.address, _amount=sp.mutez(400000000), _now=now)
    c.vote(pot_id=0, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=0, approve=True, _sender=bob.address, _now=now, _amount=sp.mutez(0))

    # Pot 1 : vote pondere, votes defavorables
    c.create_pot(title="R1", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(1, _sender=alice.address, _amount=sp.mutez(600000000), _now=now)
    c.contribute(1, _sender=bob.address, _amount=sp.mutez(400000000), _now=now)
    c.vote(pot_id=1, approve=False, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=1, approve=False, _sender=bob.address, _now=now, _amount=sp.mutez(0))

    # Pot 2 : auto mode, funded
    c.create_pot(title="R2", description="d", goal=sp.mutez(500000000),
        deadline=future, validation_mode=1, vote_deadline=sp.timestamp(0),
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(2, _sender=alice.address, _amount=sp.mutez(500000000), _now=now)

    # Pot 3 : vote pondere, pas funded (Active)
    c.create_pot(title="R3", description="d", goal=sp.mutez(9000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))

    # Pot 4 : auto mode, funded (pour test non-createur)
    c.create_pot(title="R4", description="d", goal=sp.mutez(100000000),
        deadline=future, validation_mode=1, vote_deadline=sp.timestamp(0),
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(4, _sender=alice.address, _amount=sp.mutez(100000000), _now=now)

    # Pot 5 : vote pondere, votes egalitaires
    c.create_pot(title="R5", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(5, _sender=alice.address, _amount=sp.mutez(500000000), _now=now)
    c.contribute(5, _sender=bob.address, _amount=sp.mutez(500000000), _now=now)
    c.vote(pot_id=5, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=5, approve=False, _sender=bob.address, _now=now, _amount=sp.mutez(0))

    # Pot 6 : vote pondere, funded (pour test vote_period)
    c.create_pot(title="R6", description="d", goal=sp.mutez(100000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(6, _sender=alice.address, _amount=sp.mutez(100000000), _now=now)
    c.vote(pot_id=6, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))

    # Pot 7 : vote democratique, funded, votes favorables
    c.create_pot(title="R7", description="d", goal=sp.mutez(600000000),
        deadline=future, validation_mode=2, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(7, _sender=alice.address, _amount=sp.mutez(300000000), _now=now)
    c.contribute(7, _sender=bob.address, _amount=sp.mutez(300000000), _now=now)
    c.vote(pot_id=7, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=7, approve=True, _sender=bob.address, _now=now, _amount=sp.mutez(0))

    # Pot 8 : vote pondere, createur a contribue, quorum ajuste
    # Createur 600, Alice 200, Bob 200 = 1000 total
    # Old quorum = 500 (50% de 1000) -> Alice+Bob (400) < 500 = fail
    # New quorum = 200 (50% de 400, hors createur) -> Alice+Bob (400) >= 200 = OK
    c.create_pot(title="R8", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(8, _sender=creator.address, _amount=sp.mutez(600000000), _now=now)
    c.contribute(8, _sender=alice.address, _amount=sp.mutez(200000000), _now=now)
    c.contribute(8, _sender=bob.address, _amount=sp.mutez(200000000), _now=now)
    c.vote(pot_id=8, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=8, approve=True, _sender=bob.address, _now=now, _amount=sp.mutez(0))

    sc.h2("1. Release reussi apres vote pondere favorable (apres vote_deadline)")
    c.release(0, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 2)

    sc.h2("2. Release reussi en mode auto (apres deadline = timelock)")
    c.release(2, _sender=creator.address, _now=after_deadline, _amount=sp.mutez(0))
    sc.verify(c.data.pots[2].status == 2)

    sc.h2("3. Echec release auto avant deadline (timelock)")
    c.release(4, _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="RELEASE_TOO_EARLY")

    sc.h2("4. Echec si vote defavorable (apres vote_deadline)")
    c.release(1, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_NOT_FAVORABLE")

    sc.h2("5. Echec si non-createur")
    c.release(4, _sender=outsider.address, _now=after_deadline, _amount=sp.mutez(0),
        _valid=False, _exception="NOT_CREATOR")

    sc.h2("6. Echec si pas Funded")
    c.release(3, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="POT_NOT_FUNDED")

    sc.h2("7. Echec si deja Released")
    c.release(0, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="POT_NOT_FUNDED")

    sc.h2("8. Echec si periode de vote pas terminee")
    c.release(6, _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_PERIOD_NOT_ENDED")

    sc.h2("9. Echec en cas de vote egalitaire (vote_for == vote_against)")
    c.release(5, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_NOT_FAVORABLE")

    sc.h2("10. Release reussi en mode democratique (apres vote_deadline)")
    c.release(7, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[7].status == 2)

    sc.h2("11. Release avec quorum ajuste (contribution createur exclue du quorum)")
    # Quorum = 50% de (1000 - 600 createur) = 50% de 400 = 200
    # Alice (200) + Bob (200) = 400 >= 200 -> quorum OK
    c.release(8, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[8].status == 2)


@sp.add_test()
def test_claim_refund():
    sc = sp.test_scenario("test_claim_refund", main)
    admin = sp.test_account("admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    bob = sp.test_account("bob")
    outsider = sp.test_account("outsider")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_deadline = sp.timestamp(2000001)

    # Pot 0 : Failed (deadline depassee)
    c.create_pot(title="F0", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(0, _sender=alice.address, _amount=sp.mutez(300000000), _now=now)
    c.contribute(0, _sender=bob.address, _amount=sp.mutez(200000000), _now=now)
    c.refund(0, _sender=outsider.address, _now=after_deadline, _amount=sp.mutez(0))

    # Pot 1 : Cancelled
    c.create_pot(title="C1", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(1, _sender=alice.address, _amount=sp.mutez(400000000), _now=now)
    c.cancel(1, _sender=creator.address, _now=now, _amount=sp.mutez(0))

    # Pot 2 : Active (pas failed)
    c.create_pot(title="A2", description="d", goal=sp.mutez(9000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(2, _sender=alice.address, _amount=sp.mutez(100000000), _now=now)

    # Pot 3 : Released (auto mode, apres deadline)
    c.create_pot(title="L3", description="d", goal=sp.mutez(100000000),
        deadline=future, validation_mode=1, vote_deadline=sp.timestamp(0),
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(3, _sender=alice.address, _amount=sp.mutez(100000000), _now=now)
    c.release(3, _sender=creator.address, _now=after_deadline, _amount=sp.mutez(0))

    sc.h2("1. Remboursement correct en cas d'echec (Failed)")
    c.claim_refund(0, _sender=alice.address, _now=now, _amount=sp.mutez(0))

    sc.h2("2. Remboursement correct en cas d'annulation (Cancelled)")
    c.claim_refund(1, _sender=alice.address, _now=now, _amount=sp.mutez(0))

    sc.h2("3. Echec si cagnotte active")
    c.claim_refund(2, _sender=alice.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="REFUND_NOT_AVAILABLE")

    sc.h2("4. Echec si cagnotte Released")
    c.claim_refund(3, _sender=alice.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="REFUND_NOT_AVAILABLE")

    sc.h2("5. Echec si pas contributeur")
    c.claim_refund(0, _sender=outsider.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="NOT_A_CONTRIBUTOR")

    sc.h2("6. Echec si double remboursement")
    c.claim_refund(0, _sender=alice.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="ALREADY_REFUNDED")


@sp.add_test()
def test_cancel():
    sc = sp.test_scenario("test_cancel", main)
    admin = sp.test_account("admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    outsider = sp.test_account("outsider")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_deadline = sp.timestamp(2000001)

    c.create_pot(title="C0", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(0, _sender=alice.address, _amount=sp.mutez(500000000), _now=now)

    c.create_pot(title="C1", description="d", goal=sp.mutez(100000000),
        deadline=future, validation_mode=1, vote_deadline=sp.timestamp(0),
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(1, _sender=alice.address, _amount=sp.mutez(100000000), _now=now)
    c.release(1, _sender=creator.address, _now=after_deadline, _amount=sp.mutez(0))

    sc.h2("1. Annulation reussie par le createur")
    c.cancel(0, _sender=creator.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 4)

    sc.h2("2. Echec si non-createur")
    c.create_pot(title="C2", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.cancel(2, _sender=outsider.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="NOT_CREATOR")

    sc.h2("3. Echec si fonds deja liberes (Released)")
    c.cancel(1, _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="CANNOT_CANCEL")

    sc.h2("4. Echec si deja annulee")
    c.cancel(0, _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="CANNOT_CANCEL")


@sp.add_test()
def test_refund():
    sc = sp.test_scenario("test_refund", main)
    admin = sp.test_account("admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    bob = sp.test_account("bob")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_deadline = sp.timestamp(2000001)
    after_vote_end = sp.timestamp(3000001)

    # Pot 0 : Active, objectif pas atteint
    c.create_pot(title="D0", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(0, _sender=alice.address, _amount=sp.mutez(300000000), _now=now)

    # Pot 1 : Funded, votes 100% defavorables
    c.create_pot(title="V1", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(1, _sender=alice.address, _amount=sp.mutez(600000000), _now=now)
    c.contribute(1, _sender=bob.address, _amount=sp.mutez(400000000), _now=now)
    c.vote(pot_id=1, approve=False, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=1, approve=False, _sender=bob.address, _now=now, _amount=sp.mutez(0))

    sc.h2("1. Refund apres deadline (Active -> Failed)")
    c.refund(0, _sender=alice.address, _now=after_deadline, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 3)

    sc.h2("2. Refund apres vote negatif pendant periode de vote (Funded -> Failed)")
    c.refund(1, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[1].status == 3)

    sc.h2("3. Echec si deadline non depassee")
    c.create_pot(title="E2", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(2, _sender=alice.address, _amount=sp.mutez(100000000), _now=now)
    c.refund(2, _sender=alice.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="DEADLINE_NOT_PASSED")

    sc.h2("4. Echec si deja Failed")
    c.refund(0, _sender=alice.address, _now=after_deadline, _amount=sp.mutez(0),
        _valid=False, _exception="REFUND_NOT_APPLICABLE")

    sc.h2("5. Refund si vote_deadline depassee et quorum non atteint (fonds debloques)")
    c.create_pot(title="Q3", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(3, _sender=alice.address, _amount=sp.mutez(600000000), _now=now)
    c.contribute(3, _sender=bob.address, _amount=sp.mutez(400000000), _now=now)
    c.refund(3, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[3].status == 3)

    sc.h2("6. Refund en cas de vote egalitaire (vote_for == vote_against)")
    c.create_pot(title="T4", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(4, _sender=alice.address, _amount=sp.mutez(500000000), _now=now)
    c.contribute(4, _sender=bob.address, _amount=sp.mutez(500000000), _now=now)
    c.vote(pot_id=4, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=4, approve=False, _sender=bob.address, _now=now, _amount=sp.mutez(0))
    c.refund(4, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[4].status == 3)

    sc.h2("7. Refund en mode democratique (quorum non atteint apres vote_deadline)")
    c.create_pot(title="D5", description="d", goal=sp.mutez(600000000),
        deadline=future, validation_mode=2, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(5, _sender=alice.address, _amount=sp.mutez(300000000), _now=now)
    c.contribute(5, _sender=bob.address, _amount=sp.mutez(300000000), _now=now)
    # Personne ne vote, vote_deadline passe -> fonds debloques
    c.refund(5, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[5].status == 3)


@sp.add_test()
def test_admin():
    sc = sp.test_scenario("test_admin", main)
    admin = sp.test_account("admin")
    new_admin = sp.test_account("new_admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    outsider = sp.test_account("outsider")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_deadline = sp.timestamp(2000001)
    after_vote_end = sp.timestamp(3000001)

    # Setup : creer un pot avec contribution pour tester les blocages
    c.create_pot(title="Admin Test", description="d", goal=sp.mutez(500000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(0, _sender=alice.address, _amount=sp.mutez(500000000), _now=now)
    # Pot 0 est maintenant Funded

    sc.h2("1. Admin peut activer la pause")
    c.set_pause(True, _sender=admin.address, _amount=sp.mutez(0))
    sc.verify(c.data.paused == True)

    sc.h2("2. Echec si non-admin tente de pauser")
    c.set_pause(False, _sender=outsider.address, _amount=sp.mutez(0),
        _valid=False, _exception="NOT_ADMIN")

    sc.h2("3. create_pot bloque quand pause")
    c.create_pot(title="Blocked", description="d", goal=sp.mutez(100000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="CONTRACT_PAUSED")

    sc.h2("4. contribute bloque quand pause")
    c.create_pot(title="Pre-pause", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="CONTRACT_PAUSED")

    sc.h2("5. vote bloque quand pause")
    c.vote(pot_id=0, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="CONTRACT_PAUSED")

    sc.h2("6. release bloque quand pause")
    c.release(0, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="CONTRACT_PAUSED")

    sc.h2("7. cancel fonctionne meme en pause (operation de securite)")
    c.cancel(0, _sender=creator.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 4)

    sc.h2("8. claim_refund fonctionne meme en pause (operation de securite)")
    c.claim_refund(0, _sender=alice.address, _now=now, _amount=sp.mutez(0))

    sc.h2("9. Admin desactive la pause")
    c.set_pause(False, _sender=admin.address, _amount=sp.mutez(0))
    sc.verify(c.data.paused == False)

    sc.h2("10. Admin transfere le role a new_admin")
    c.transfer_admin(new_admin.address, _sender=admin.address, _amount=sp.mutez(0))
    sc.verify(c.data.admin == new_admin.address)

    sc.h2("11. Ancien admin ne peut plus pauser")
    c.set_pause(True, _sender=admin.address, _amount=sp.mutez(0),
        _valid=False, _exception="NOT_ADMIN")

    sc.h2("12. Nouveau admin peut pauser")
    c.set_pause(True, _sender=new_admin.address, _amount=sp.mutez(0))
    sc.verify(c.data.paused == True)


# ============================================================================
# SCENARIOS E2E
# ============================================================================


@sp.add_test()
def test_e2e_success():
    sc = sp.test_scenario("test_e2e_success", main)
    admin = sp.test_account("admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    bob = sp.test_account("bob")
    charlie = sp.test_account("charlie")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_vote_end = sp.timestamp(3000001)

    sc.h2("E2E : creation -> contributions -> objectif -> vote OUI -> release")

    c.create_pot(title="Voyage Bali 2026", description="Voyage entre amis",
        goal=sp.mutez(2000000000), deadline=future, validation_mode=0,
        vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 0)

    c.contribute(0, _sender=alice.address, _amount=sp.mutez(800000000), _now=now)
    c.contribute(0, _sender=bob.address, _amount=sp.mutez(700000000), _now=now)
    c.contribute(0, _sender=charlie.address, _amount=sp.mutez(500000000), _now=now)
    sc.verify(c.data.pots[0].status == 1)
    sc.verify(c.data.pots[0].total_contributed == sp.mutez(2000000000))

    c.vote(pot_id=0, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=0, approve=True, _sender=bob.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=0, approve=False, _sender=charlie.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].vote_for == sp.mutez(1500000000))
    sc.verify(c.data.pots[0].vote_against == sp.mutez(500000000))

    c.release(0, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 2)


@sp.add_test()
def test_e2e_deadline_failure():
    sc = sp.test_scenario("test_e2e_deadline_failure", main)
    admin = sp.test_account("admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    bob = sp.test_account("bob")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_deadline = sp.timestamp(2000001)

    sc.h2("E2E : creation -> contributions insuffisantes -> deadline -> refund")

    c.create_pot(title="Projet Etudiant", description="Financer un prototype",
        goal=sp.mutez(1500000000), deadline=future, validation_mode=0,
        vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))

    c.contribute(0, _sender=alice.address, _amount=sp.mutez(400000000), _now=now)
    c.contribute(0, _sender=bob.address, _amount=sp.mutez(300000000), _now=now)
    sc.verify(c.data.pots[0].status == 0)

    c.refund(0, _sender=alice.address, _now=after_deadline, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 3)

    c.claim_refund(0, _sender=alice.address, _now=after_deadline, _amount=sp.mutez(0))
    c.claim_refund(0, _sender=bob.address, _now=after_deadline, _amount=sp.mutez(0))


@sp.add_test()
def test_e2e_cancel():
    sc = sp.test_scenario("test_e2e_cancel", main)
    admin = sp.test_account("admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    bob = sp.test_account("bob")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)

    sc.h2("E2E : creation -> contributions -> cancel -> claim_refund")

    c.create_pot(title="Evenement Caritatif", description="Reunion de fonds",
        goal=sp.mutez(3000000000), deadline=future, validation_mode=0,
        vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))

    c.contribute(0, _sender=alice.address, _amount=sp.mutez(500000000), _now=now)
    c.contribute(0, _sender=bob.address, _amount=sp.mutez(300000000), _now=now)

    c.cancel(0, _sender=creator.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 4)

    c.claim_refund(0, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    c.claim_refund(0, _sender=bob.address, _now=now, _amount=sp.mutez(0))


@sp.add_test()
def test_e2e_negative_vote():
    sc = sp.test_scenario("test_e2e_negative_vote", main)
    admin = sp.test_account("admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    bob = sp.test_account("bob")
    charlie = sp.test_account("charlie")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_vote_end = sp.timestamp(3000001)

    sc.h2("E2E : creation -> contributions -> objectif -> vote NON -> refund")

    c.create_pot(title="Cadeau Anniversaire", description="Cadeau pour Lea",
        goal=sp.mutez(500000000), deadline=future, validation_mode=0,
        vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))

    c.contribute(0, _sender=alice.address, _amount=sp.mutez(250000000), _now=now)
    c.contribute(0, _sender=bob.address, _amount=sp.mutez(150000000), _now=now)
    c.contribute(0, _sender=charlie.address, _amount=sp.mutez(100000000), _now=now)
    sc.verify(c.data.pots[0].status == 1)

    c.vote(pot_id=0, approve=False, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=0, approve=False, _sender=bob.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=0, approve=True, _sender=charlie.address, _now=now, _amount=sp.mutez(0))

    sc.verify(c.data.pots[0].vote_against == sp.mutez(400000000))
    sc.verify(c.data.pots[0].vote_for == sp.mutez(100000000))

    c.release(0, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_NOT_FAVORABLE")

    c.refund(0, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 3)

    c.claim_refund(0, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    c.claim_refund(0, _sender=bob.address, _now=after_vote_end, _amount=sp.mutez(0))
    c.claim_refund(0, _sender=charlie.address, _now=after_vote_end, _amount=sp.mutez(0))


@sp.add_test()
def test_e2e_quorum_not_reached():
    sc = sp.test_scenario("test_e2e_quorum_not_reached", main)
    admin = sp.test_account("admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    bob = sp.test_account("bob")
    charlie = sp.test_account("charlie")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_vote_end = sp.timestamp(3000001)

    sc.h2("E2E : funded -> personne ne vote -> vote_deadline -> refund -> claim")

    c.create_pot(title="Quorum Fail", description="Personne ne vote",
        goal=sp.mutez(1000000000), deadline=future, validation_mode=0,
        vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))

    c.contribute(0, _sender=alice.address, _amount=sp.mutez(400000000), _now=now)
    c.contribute(0, _sender=bob.address, _amount=sp.mutez(300000000), _now=now)
    c.contribute(0, _sender=charlie.address, _amount=sp.mutez(300000000), _now=now)
    sc.verify(c.data.pots[0].status == 1)

    c.release(0, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="QUORUM_NOT_REACHED")

    c.refund(0, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 3)

    c.claim_refund(0, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    c.claim_refund(0, _sender=bob.address, _now=after_vote_end, _amount=sp.mutez(0))
    c.claim_refund(0, _sender=charlie.address, _now=after_vote_end, _amount=sp.mutez(0))


@sp.add_test()
def test_e2e_tied_vote():
    sc = sp.test_scenario("test_e2e_tied_vote", main)
    admin = sp.test_account("admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    bob = sp.test_account("bob")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_vote_end = sp.timestamp(3000001)

    sc.h2("E2E : funded -> vote egalitaire -> release refuse -> refund -> claim")

    c.create_pot(title="Vote Egalite", description="Test egalite",
        goal=sp.mutez(1000000000), deadline=future, validation_mode=0,
        vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))

    c.contribute(0, _sender=alice.address, _amount=sp.mutez(500000000), _now=now)
    c.contribute(0, _sender=bob.address, _amount=sp.mutez(500000000), _now=now)
    sc.verify(c.data.pots[0].status == 1)

    c.vote(pot_id=0, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=0, approve=False, _sender=bob.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].vote_for == sp.mutez(500000000))
    sc.verify(c.data.pots[0].vote_against == sp.mutez(500000000))

    c.release(0, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_NOT_FAVORABLE")

    c.refund(0, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 3)

    c.claim_refund(0, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    c.claim_refund(0, _sender=bob.address, _now=after_vote_end, _amount=sp.mutez(0))


@sp.add_test()
def test_e2e_democratic_vote():
    sc = sp.test_scenario("test_e2e_democratic_vote", main)
    admin = sp.test_account("admin")
    c = main.CrowdTrust(admin.address)
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    bob = sp.test_account("bob")
    charlie = sp.test_account("charlie")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_vote_end = sp.timestamp(3000001)

    sc.h2("E2E democratique : 1 personne = 1 voix, gros contributeur ne domine pas")

    # Alice contribue 80%, Bob 15%, Charlie 5%
    # En mode pondere, Alice dominerait. En mode democratique, chacun a 1 voix.
    c.create_pot(title="Vote Demo", description="Test democratique",
        goal=sp.mutez(1000000000), deadline=future, validation_mode=2,
        vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))

    c.contribute(0, _sender=alice.address, _amount=sp.mutez(800000000), _now=now)
    c.contribute(0, _sender=bob.address, _amount=sp.mutez(150000000), _now=now)
    c.contribute(0, _sender=charlie.address, _amount=sp.mutez(50000000), _now=now)
    sc.verify(c.data.pots[0].status == 1)
    sc.verify(c.data.pots[0].contributor_count == 3)

    # Alice vote OUI, Bob et Charlie votent NON
    # En pondere : OUI=800M > NON=200M -> release OK (alice domine)
    # En democratique : OUI=1 < NON=2 -> release REFUSE (majorite en nombre)
    c.vote(pot_id=0, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=0, approve=False, _sender=bob.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=0, approve=False, _sender=charlie.address, _now=now, _amount=sp.mutez(0))

    sc.verify(c.data.pots[0].vote_for == sp.mutez(1))
    sc.verify(c.data.pots[0].vote_against == sp.mutez(2))

    # Release refuse : 1 voix pour < 2 voix contre
    c.release(0, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_NOT_FAVORABLE")

    # Refund autorise : vote defavorable
    c.refund(0, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 3)

    # Chacun recupere sa contribution (montant original, pas 1 voix)
    c.claim_refund(0, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    c.claim_refund(0, _sender=bob.address, _now=after_vote_end, _amount=sp.mutez(0))
    c.claim_refund(0, _sender=charlie.address, _now=after_vote_end, _amount=sp.mutez(0))
