import smartpy as sp

# ============================================================================
# CrowdTrust - Cagnotte intelligente on-chain avec escrow et gouvernance
# ============================================================================
# Statuts : 0=Active, 1=Funded, 2=Released, 3=Failed, 4=Cancelled
# Validation modes : 0=Vote, 1=Auto
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
#   (vote_against >= vote_for). Ce choix conservateur protege les contributeurs
#   en cas d'ambiguite.
#
# Protection anti-manipulation du createur :
#   Le createur peut contribuer a sa propre cagnotte (comportement normal,
#   comme sur Leetchi : ex. pot de depart en retraite), MAIS il ne peut PAS
#   voter. Cela empeche le createur de s'assurer une majorite via une grosse
#   contribution puis un vote en sa faveur.
#
# Vote deadline :
#   En mode vote, une vote_deadline (apres la deadline de contribution) est
#   obligatoire. Apres cette date, si le quorum n'est pas atteint ou si le
#   vote est defavorable/egalitaire, n'importe qui peut appeler refund()
#   pour debloquer les fonds. Cela evite que les fonds restent bloques
#   indefiniment si les contributeurs ne votent pas.
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
        status=sp.nat,
        vote_for=sp.mutez,
        vote_against=sp.mutez,
        validation_mode=sp.nat,
    )

    class CrowdTrust(sp.Contract):
        def __init__(self):
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

        # ----------------------------------------------------------------
        # create_pot : cree une nouvelle cagnotte
        # - Limites : titre <= 50 car, description <= 500 car
        # - Mode vote : vote_deadline doit etre apres deadline
        # - Mode auto : vote_deadline ignoree (stocker n'importe quelle valeur)
        # ----------------------------------------------------------------
        @sp.entrypoint
        def create_pot(self, title, description, goal, deadline, validation_mode, vote_deadline):
            sp.cast(title, sp.string)
            sp.cast(description, sp.string)
            sp.cast(goal, sp.mutez)
            sp.cast(deadline, sp.timestamp)
            sp.cast(validation_mode, sp.nat)
            sp.cast(vote_deadline, sp.timestamp)

            assert goal > sp.mutez(0), "GOAL_MUST_BE_POSITIVE"
            assert deadline > sp.now, "DEADLINE_MUST_BE_FUTURE"
            assert validation_mode <= 1, "INVALID_VALIDATION_MODE"
            assert sp.amount == sp.mutez(0), "NO_XTZ_ON_CREATE"
            assert sp.len(title) > 0, "TITLE_EMPTY"
            assert sp.len(title) <= 50, "TITLE_TOO_LONG"
            assert sp.len(description) <= 500, "DESCRIPTION_TOO_LONG"

            if validation_mode == 0:
                assert vote_deadline > deadline, "VOTE_DEADLINE_MUST_BE_AFTER_DEADLINE"

            self.data.pots[self.data.next_id] = sp.record(
                creator=sp.sender,
                title=title,
                description=description,
                goal=goal,
                deadline=deadline,
                vote_deadline=vote_deadline,
                total_contributed=sp.mutez(0),
                status=0,
                vote_for=sp.mutez(0),
                vote_against=sp.mutez(0),
                validation_mode=validation_mode,
            )
            self.data.next_id += 1

            sp.emit(sp.record(creator=sp.sender, goal=goal), tag="PotCreated")

        # ----------------------------------------------------------------
        # contribute : contribuer a une cagnotte active
        # ----------------------------------------------------------------
        @sp.entrypoint
        def contribute(self, pot_id):
            sp.cast(pot_id, sp.nat)
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

            if pot.total_contributed >= pot.goal:
                pot.status = 1
            self.data.pots[pot_id] = pot

            sp.emit(sp.record(pot_id=pot_id, contributor=sp.sender, amount=sp.amount), tag="Contribution")

        # ----------------------------------------------------------------
        # vote : voter pour ou contre la liberation des fonds
        # - Vote pondere par le montant de la contribution
        # - Le CREATEUR ne peut PAS voter (anti-manipulation)
        # - Le vote est possible uniquement pendant la periode de vote
        #   (apres Funded, avant vote_deadline)
        #
        # REGLE D'EGALITE : en cas de vote_for == vote_against, le
        # resultat est considere comme DEFAVORABLE. Le release est refuse
        # (vote_for > vote_against requis, strictement superieur) et le
        # refund est autorise (vote_against >= vote_for inclut l'egalite).
        # Ce choix conservateur protege les contributeurs en cas d'ambiguite.
        # ----------------------------------------------------------------
        @sp.entrypoint
        def vote(self, pot_id, approve):
            sp.cast(pot_id, sp.nat)
            sp.cast(approve, sp.bool)
            assert self.data.pots.contains(pot_id), "POT_NOT_FOUND"
            pot = self.data.pots[pot_id]
            assert pot.status == 1, "POT_NOT_FUNDED"
            assert pot.validation_mode == 0, "POT_IS_AUTO_MODE"
            assert sp.amount == sp.mutez(0), "NO_XTZ_ON_VOTE"
            assert sp.sender != pot.creator, "CREATOR_CANNOT_VOTE"
            assert sp.now <= pot.vote_deadline, "VOTE_PERIOD_ENDED"

            key = (pot_id, sp.sender)
            assert self.data.contributions.contains(key), "NOT_A_CONTRIBUTOR"
            assert self.data.votes.contains(key) == False, "ALREADY_VOTED"

            contribution = self.data.contributions[key]
            self.data.votes[key] = approve

            if approve:
                pot.vote_for += contribution
            else:
                pot.vote_against += contribution
            self.data.pots[pot_id] = pot

            sp.emit(sp.record(pot_id=pot_id, voter=sp.sender, approve=approve), tag="VoteCast")

        # ----------------------------------------------------------------
        # release : liberer les fonds vers le createur
        # Mode vote : necessite que la vote_deadline soit passee,
        #             quorum (50%) atteint, et vote_for > vote_against
        # Mode auto : liberation immediate apres Funded
        #
        # EGALITE : vote_for == vote_against -> release REFUSE
        # Le createur doit attendre la fin de la periode de vote.
        # ----------------------------------------------------------------
        @sp.entrypoint
        def release(self, pot_id):
            sp.cast(pot_id, sp.nat)
            assert self.data.pots.contains(pot_id), "POT_NOT_FOUND"
            pot = self.data.pots[pot_id]
            assert pot.status == 1, "POT_NOT_FUNDED"
            assert sp.sender == pot.creator, "NOT_CREATOR"
            assert sp.amount == sp.mutez(0), "NO_XTZ_ON_RELEASE"

            if pot.validation_mode == 0:
                assert sp.now > pot.vote_deadline, "VOTE_PERIOD_NOT_ENDED"
                total_votes = pot.vote_for + pot.vote_against
                quorum_threshold = sp.split_tokens(
                    pot.total_contributed, 50, 100
                )
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
        # Cas 2 : Funded + vote mode :
        #   - Periode de vote en cours : quorum atteint + vote defavorable
        #   - Periode de vote terminee :
        #       * Quorum atteint + vote defavorable/egal -> Failed
        #       * Quorum NON atteint -> Failed (deblocage des fonds)
        #
        # EGALITE : vote_against >= vote_for -> refund AUTORISE
        # Cela inclut le cas egalitaire (vote_for == vote_against).
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
                    assert pot.validation_mode == 0, "POT_IS_AUTO_MODE"
                    total_votes = pot.vote_for + pot.vote_against
                    quorum_threshold = sp.split_tokens(
                        pot.total_contributed, 50, 100
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
                        # Seul un quorum atteint + vote defavorable permet
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
#   during_vote    = 2 500 000   (pendant la periode de vote)
#
# Entrypoints a 1 param (contribute, release, claim_refund, cancel, refund) :
#   -> appel positionnel : c.contribute(0, _sender=...)
# Entrypoints a N params (create_pot, vote) :
#   -> appel keyword : c.vote(pot_id=0, approve=True, _sender=...)
# ============================================================================


@sp.add_test()
def test_create_pot():
    sc = sp.test_scenario("test_create_pot", main)
    c = main.CrowdTrust()
    sc += c

    creator = sp.test_account("creator")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    past = sp.timestamp(500000)
    vote_end = sp.timestamp(3000000)

    sc.h2("1. Creation reussie avec parametres valides (mode vote)")
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

    sc.h2("3. Echec si objectif = 0")
    c.create_pot(
        title="Test", description="t", goal=sp.mutez(0), deadline=future,
        validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="GOAL_MUST_BE_POSITIVE",
    )

    sc.h2("4. Echec si deadline dans le passe")
    c.create_pot(
        title="Test", description="t", goal=sp.mutez(1000000), deadline=past,
        validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="DEADLINE_MUST_BE_FUTURE",
    )

    sc.h2("5. Echec si validation_mode invalide")
    c.create_pot(
        title="Test", description="t", goal=sp.mutez(1000000), deadline=future,
        validation_mode=5, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="INVALID_VALIDATION_MODE",
    )

    sc.h2("6. Echec si titre trop long (> 50 caracteres)")
    c.create_pot(
        title="A" * 51, description="t", goal=sp.mutez(1000000), deadline=future,
        validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="TITLE_TOO_LONG",
    )

    sc.h2("7. Echec si description trop longue (> 500 caracteres)")
    c.create_pot(
        title="Test", description="A" * 501, goal=sp.mutez(1000000), deadline=future,
        validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="DESCRIPTION_TOO_LONG",
    )

    sc.h2("8. Echec si vote_deadline <= deadline en mode vote")
    c.create_pot(
        title="Test", description="t", goal=sp.mutez(1000000), deadline=future,
        validation_mode=0, vote_deadline=future,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_DEADLINE_MUST_BE_AFTER_DEADLINE",
    )

    sc.h2("9. Echec si titre vide")
    c.create_pot(
        title="", description="t", goal=sp.mutez(1000000), deadline=future,
        validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="TITLE_EMPTY",
    )


@sp.add_test()
def test_contribute():
    sc = sp.test_scenario("test_contribute", main)
    c = main.CrowdTrust()
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

    sc.h2("2. Contributions multiples du meme utilisateur (cumul)")
    c.contribute(0, _sender=alice.address, _amount=sp.mutez(200000000), _now=now)
    sc.verify(c.data.pots[0].total_contributed == sp.mutez(700000000))

    sc.h2("3. Verification total_contributed mis a jour")
    c.contribute(0, _sender=bob.address, _amount=sp.mutez(300000000), _now=now)
    sc.verify(c.data.pots[0].total_contributed == sp.mutez(1000000000))

    sc.h2("4. Passage au statut Funded si objectif atteint")
    sc.verify(c.data.pots[0].status == 1)

    sc.h2("5. Echec si montant = 0")
    c.contribute(1, _sender=alice.address, _amount=sp.mutez(0), _now=now,
        _valid=False, _exception="CONTRIBUTION_MUST_BE_POSITIVE")

    sc.h2("6. Echec si cagnotte inexistante")
    c.contribute(999, _sender=alice.address, _amount=sp.mutez(100000), _now=now,
        _valid=False, _exception="POT_NOT_FOUND")

    sc.h2("7. Echec si deadline depassee")
    c.contribute(1, _sender=alice.address, _amount=sp.mutez(100000), _now=after_deadline,
        _valid=False, _exception="DEADLINE_PASSED")

    sc.h2("8. Passage automatique au statut Funded (mode auto)")
    c.contribute(1, _sender=bob.address, _amount=sp.mutez(500000000), _now=now)
    sc.verify(c.data.pots[1].status == 1)

    sc.h2("9. Echec si cagnotte non active (Funded)")
    c.contribute(1, _sender=charlie.address, _amount=sp.mutez(100000), _now=now,
        _valid=False, _exception="POT_NOT_ACTIVE")



@sp.add_test()
def test_vote():
    sc = sp.test_scenario("test_vote", main)
    c = main.CrowdTrust()
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

    # Pot 0 : vote mode, funded par alice+bob+charlie
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

    # Pot 2 : vote mode, pas encore funded (Active)
    c.create_pot(title="Active Pot", description="d", goal=sp.mutez(9000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(2, _sender=alice.address, _amount=sp.mutez(100000000), _now=now)

    # Pot 3 : vote mode, funded, createur a contribue
    c.create_pot(title="Creator Pot", description="d", goal=sp.mutez(500000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(3, _sender=creator.address, _amount=sp.mutez(300000000), _now=now)
    c.contribute(3, _sender=alice.address, _amount=sp.mutez(200000000), _now=now)

    sc.h2("1. Vote favorable enregistre correctement")
    c.vote(pot_id=0, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].vote_for == sp.mutez(500000000))

    sc.h2("2. Vote defavorable enregistre correctement")
    c.vote(pot_id=0, approve=False, _sender=charlie.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].vote_against == sp.mutez(150000000))

    sc.h2("3. Verification poids et cumul vote_for / vote_against")
    c.vote(pot_id=0, approve=True, _sender=bob.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].vote_for == sp.mutez(850000000))
    sc.verify(c.data.pots[0].vote_against == sp.mutez(150000000))

    sc.h2("4. Echec si pas contributeur")
    c.vote(pot_id=0, approve=True, _sender=outsider.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="NOT_A_CONTRIBUTOR")

    sc.h2("5. Echec si double vote")
    c.vote(pot_id=0, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="ALREADY_VOTED")

    sc.h2("6. Echec si cagnotte pas en statut Funded")
    c.vote(pot_id=2, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="POT_NOT_FUNDED")

    sc.h2("7. Echec si mode auto")
    c.vote(pot_id=1, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="POT_IS_AUTO_MODE")

    sc.h2("8. Echec si le createur tente de voter (anti-manipulation)")
    c.vote(pot_id=3, approve=True, _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="CREATOR_CANNOT_VOTE")

    sc.h2("9. Echec si periode de vote terminee (apres vote_deadline)")
    c.vote(pot_id=3, approve=True, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_PERIOD_ENDED")


@sp.add_test()
def test_release():
    sc = sp.test_scenario("test_release", main)
    c = main.CrowdTrust()
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    bob = sp.test_account("bob")
    outsider = sp.test_account("outsider")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_vote_end = sp.timestamp(3000001)

    # Pot 0 : vote mode, votes favorables
    c.create_pot(title="R0", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(0, _sender=alice.address, _amount=sp.mutez(600000000), _now=now)
    c.contribute(0, _sender=bob.address, _amount=sp.mutez(400000000), _now=now)
    c.vote(pot_id=0, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=0, approve=True, _sender=bob.address, _now=now, _amount=sp.mutez(0))

    # Pot 1 : vote mode, votes defavorables
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

    # Pot 3 : vote mode, pas funded (Active)
    c.create_pot(title="R3", description="d", goal=sp.mutez(9000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))

    # Pot 4 : auto mode, funded (pour test non-createur)
    c.create_pot(title="R4", description="d", goal=sp.mutez(100000000),
        deadline=future, validation_mode=1, vote_deadline=sp.timestamp(0),
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(4, _sender=alice.address, _amount=sp.mutez(100000000), _now=now)

    # Pot 5 : vote mode, votes egalitaires (pour test egalite)
    c.create_pot(title="R5", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(5, _sender=alice.address, _amount=sp.mutez(500000000), _now=now)
    c.contribute(5, _sender=bob.address, _amount=sp.mutez(500000000), _now=now)
    c.vote(pot_id=5, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))
    c.vote(pot_id=5, approve=False, _sender=bob.address, _now=now, _amount=sp.mutez(0))

    # Pot 6 : vote mode, funded, votes favorables (pour test vote_period)
    c.create_pot(title="R6", description="d", goal=sp.mutez(100000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(6, _sender=alice.address, _amount=sp.mutez(100000000), _now=now)
    c.vote(pot_id=6, approve=True, _sender=alice.address, _now=now, _amount=sp.mutez(0))

    sc.h2("1. Release reussi apres vote majoritaire favorable (apres vote_deadline)")
    c.release(0, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 2)

    sc.h2("2. Release reussi en mode auto (sans vote, sans attendre)")
    c.release(2, _sender=creator.address, _now=now, _amount=sp.mutez(0))
    sc.verify(c.data.pots[2].status == 2)

    sc.h2("3. Echec si vote defavorable (apres vote_deadline)")
    c.release(1, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_NOT_FAVORABLE")

    sc.h2("4. Echec si non-createur")
    c.release(4, _sender=outsider.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="NOT_CREATOR")

    sc.h2("5. Echec si pas Funded")
    c.release(3, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="POT_NOT_FUNDED")

    sc.h2("6. Echec si deja Released")
    c.release(0, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="POT_NOT_FUNDED")

    sc.h2("7. Echec si periode de vote pas terminee")
    c.release(6, _sender=creator.address, _now=now, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_PERIOD_NOT_ENDED")

    sc.h2("8. Echec en cas de vote egalitaire (vote_for == vote_against)")
    # Pot 5 : alice OUI (500M), bob NON (500M) -> egalite -> DEFAVORABLE
    c.release(5, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_NOT_FAVORABLE")


@sp.add_test()
def test_claim_refund():
    sc = sp.test_scenario("test_claim_refund", main)
    c = main.CrowdTrust()
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

    # Pot 3 : Released (auto mode)
    c.create_pot(title="L3", description="d", goal=sp.mutez(100000000),
        deadline=future, validation_mode=1, vote_deadline=sp.timestamp(0),
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(3, _sender=alice.address, _amount=sp.mutez(100000000), _now=now)
    c.release(3, _sender=creator.address, _now=now, _amount=sp.mutez(0))

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
    c = main.CrowdTrust()
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    outsider = sp.test_account("outsider")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)

    c.create_pot(title="C0", description="d", goal=sp.mutez(1000000000),
        deadline=future, validation_mode=0, vote_deadline=vote_end,
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(0, _sender=alice.address, _amount=sp.mutez(500000000), _now=now)

    c.create_pot(title="C1", description="d", goal=sp.mutez(100000000),
        deadline=future, validation_mode=1, vote_deadline=sp.timestamp(0),
        _sender=creator.address, _now=now, _amount=sp.mutez(0))
    c.contribute(1, _sender=alice.address, _amount=sp.mutez(100000000), _now=now)
    c.release(1, _sender=creator.address, _now=now, _amount=sp.mutez(0))

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
    c = main.CrowdTrust()
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
    # Personne ne vote -> quorum = 0 < 50%
    # Apres vote_deadline, refund autorise malgre absence de quorum
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
    # Egalite : 500M pour, 500M contre -> vote_against >= vote_for -> refund OK
    c.refund(4, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[4].status == 3)


# ============================================================================
# SCENARIOS E2E
# ============================================================================


@sp.add_test()
def test_e2e_success():
    sc = sp.test_scenario("test_e2e_success", main)
    c = main.CrowdTrust()
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

    # Release apres la fin de la periode de vote
    c.release(0, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 2)


@sp.add_test()
def test_e2e_deadline_failure():
    sc = sp.test_scenario("test_e2e_deadline_failure", main)
    c = main.CrowdTrust()
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
    c = main.CrowdTrust()
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
    c = main.CrowdTrust()
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

    # Le release echoue car vote defavorable (apres vote_deadline)
    c.release(0, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_NOT_FAVORABLE")

    # Le refund passe car vote defavorable + quorum atteint
    c.refund(0, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 3)

    c.claim_refund(0, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    c.claim_refund(0, _sender=bob.address, _now=after_vote_end, _amount=sp.mutez(0))
    c.claim_refund(0, _sender=charlie.address, _now=after_vote_end, _amount=sp.mutez(0))


@sp.add_test()
def test_e2e_quorum_not_reached():
    sc = sp.test_scenario("test_e2e_quorum_not_reached", main)
    c = main.CrowdTrust()
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

    # Personne ne vote, vote_deadline passe
    # Release impossible : quorum non atteint
    c.release(0, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="QUORUM_NOT_REACHED")

    # Refund autorise : vote_deadline passee + quorum non atteint
    c.refund(0, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 3)

    c.claim_refund(0, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    c.claim_refund(0, _sender=bob.address, _now=after_vote_end, _amount=sp.mutez(0))
    c.claim_refund(0, _sender=charlie.address, _now=after_vote_end, _amount=sp.mutez(0))


@sp.add_test()
def test_e2e_tied_vote():
    sc = sp.test_scenario("test_e2e_tied_vote", main)
    c = main.CrowdTrust()
    sc += c

    creator = sp.test_account("creator")
    alice = sp.test_account("alice")
    bob = sp.test_account("bob")
    now = sp.timestamp(1000000)
    future = sp.timestamp(2000000)
    vote_end = sp.timestamp(3000000)
    after_vote_end = sp.timestamp(3000001)

    sc.h2("E2E : funded -> vote egalitaire -> release refuse -> refund -> claim")
    # Regle : egalite (vote_for == vote_against) = DEFAVORABLE
    # Le release est refuse (strict >), le refund est autorise (>=)

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

    # Release refuse : vote_for (500M) N'EST PAS > vote_against (500M)
    c.release(0, _sender=creator.address, _now=after_vote_end, _amount=sp.mutez(0),
        _valid=False, _exception="VOTE_NOT_FAVORABLE")

    # Refund autorise : vote_against (500M) >= vote_for (500M)
    c.refund(0, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    sc.verify(c.data.pots[0].status == 3)

    c.claim_refund(0, _sender=alice.address, _now=after_vote_end, _amount=sp.mutez(0))
    c.claim_refund(0, _sender=bob.address, _now=after_vote_end, _amount=sp.mutez(0))
