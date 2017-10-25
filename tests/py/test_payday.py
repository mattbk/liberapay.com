from __future__ import absolute_import, division, print_function, unicode_literals

from decimal import Decimal as D
import json

import mock

from liberapay.billing.payday import create_payday_issue, main, NoPayday, Payday
from liberapay.billing.transactions import create_debt
from liberapay.exceptions import NegativeBalance
from liberapay.models.participant import Participant
from liberapay.testing import EUR, Foobar
from liberapay.testing.mangopay import FakeTransfersHarness, MangopayHarness
from liberapay.testing.emails import EmailHarness


class TestPayday(EmailHarness, FakeTransfersHarness, MangopayHarness):

    def test_payday_prevents_human_errors(self):
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            lock = cursor.one("SELECT pg_try_advisory_lock(1)")
            assert lock  # sanity check
            with self.assertRaises(AssertionError) as cm:
                main()
            assert cm.exception.msg == "failed to acquire the payday lock"

        main()

        with self.assertRaises(AssertionError) as cm:
            main()
        assert cm.exception.msg == "payday has already been run this week"

        admin = self.make_participant('admin', privileges=1)
        r = self.client.PxST('/admin/payday', data={'action': 'run_payday'}, auth_as=admin)
        assert r.code == 403
        assert r.text == "it's not time to run payday"

    @mock.patch('liberapay.billing.payday.exec_payday')
    @mock.patch.object(Payday, 'transfer_for_real')
    def test_payday_can_be_restarted_after_crash(self, transfer_for_real, exec_payday):
        transfer_for_real.side_effect = Foobar
        self.janet.set_tip_to(self.homer, EUR('6.00'))
        with self.assertRaises(Foobar):
            Payday.start().run()
        # Check that the web interface allows relaunching
        admin = self.make_participant('admin', privileges=1)
        r = self.client.PxST('/admin/payday', data={'action': 'rerun_payday'}, auth_as=admin)
        assert r.code == 302
        assert exec_payday.call_count == 1
        # Test actually relaunching
        transfer_for_real.side_effect = None
        Payday.start().run()

    def test_payday_id_is_serial(self):
        for i in range(1, 4):
            self.db.run("SELECT nextval('paydays_id_seq')")
            main(override_payday_checks=True)
            id = self.db.one("SELECT id FROM paydays ORDER BY id DESC LIMIT 1")
            assert id == i

    def test_payday_moves_money(self):
        self.janet.set_tip_to(self.homer, EUR('6.00'))  # under $10!
        self.make_exchange('mango-cc', 10, 0, self.janet)
        Payday.start().run()

        janet = Participant.from_username('janet')
        homer = Participant.from_username('homer')

        assert homer.balance == D('6.00')
        assert janet.balance == D('4.00')

        assert self.transfer_mock.call_count

    def test_update_cached_amounts(self):
        team = self.make_participant('team', kind='group')
        alice = self.make_participant('alice', balance=100)
        bob = self.make_participant('bob')
        carl = self.make_participant('carl', balance=1.56)
        dana = self.make_participant('dana')
        emma = Participant.make_stub(username='emma')
        team2 = self.make_participant('team2', kind='group')
        team2.add_member(dana)
        alice.set_tip_to(dana, EUR('3.00'))
        alice.set_tip_to(bob, EUR('6.00'))
        alice.set_tip_to(emma, EUR('0.50'))
        alice.set_tip_to(team, EUR('1.20'))
        alice.set_tip_to(team2, EUR('0.49'))
        bob.set_tip_to(alice, EUR('5.00'))
        team.add_member(bob)
        team.set_take_for(bob, D('1.00'), team)
        bob.set_tip_to(dana, EUR('2.00'))  # funded by bob's take
        bob.set_tip_to(emma, EUR('7.00'))  # not funded, insufficient receiving
        carl.set_tip_to(dana, EUR('2.08'))  # not funded, insufficient balance

        def check():
            alice = Participant.from_username('alice')
            bob = Participant.from_username('bob')
            carl = Participant.from_username('carl')
            dana = Participant.from_username('dana')
            emma = Participant.from_username('emma')
            assert alice.giving == D('10.69')
            assert alice.receiving == D('5.00')
            assert alice.npatrons == 1
            assert alice.nteampatrons == 0
            assert bob.giving == D('7.00')
            assert bob.receiving == D('7.00')
            assert bob.taking == D('1.00')
            assert bob.npatrons == 1
            assert bob.nteampatrons == 1
            assert carl.giving == D('0.00')
            assert carl.receiving == D('0.00')
            assert carl.npatrons == 0
            assert carl.nteampatrons == 0
            assert dana.receiving == D('5.00')
            assert dana.npatrons == 2
            assert dana.nteampatrons == 0
            assert emma.receiving == D('0.50')
            assert emma.npatrons == 1
            assert emma.nteampatrons == 0
            funded_tips = self.db.all("SELECT amount FROM tips WHERE is_funded ORDER BY id")
            assert funded_tips == [3, 6, 0.5, D('1.20'), D('0.49'), 5, 2]

            team = Participant.from_username('team')
            assert team.receiving == D('1.20')
            assert team.npatrons == 1
            assert team.leftover == D('0.20')

            team2 = Participant.from_username('team2')
            assert team2.receiving == D('0.49')
            assert team2.npatrons == 1
            assert team2.leftover == D('0.49')

            janet = self.janet.refetch()
            assert janet.giving == 0
            assert janet.receiving == 0
            assert janet.taking == 0
            assert janet.npatrons == 0
            assert janet.nteampatrons == 0

        # Pre-test check
        check()

        # Check that update_cached_amounts doesn't mess anything up
        Payday.start().update_cached_amounts()
        check()

        # Check that update_cached_amounts actually updates amounts
        self.db.run("""
            UPDATE tips t
               SET is_funded = true
              FROM participants p
             WHERE p.id = t.tippee
               AND p.mangopay_user_id IS NOT NULL;
            UPDATE participants
               SET giving = 10000
                 , taking = 10000
             WHERE mangopay_user_id IS NOT NULL;
            UPDATE participants
               SET npatrons = 10000
                 , receiving = 10000
             WHERE status = 'active';
        """)
        Payday.start().update_cached_amounts()
        check()

        # Check that the update methods of Participant concur
        for p in self.db.all("SELECT p.*::participants FROM participants p"):
            p.update_receiving()
            p.update_giving()
        check()

    def test_update_cached_amounts_depth(self):
        alice = self.make_participant('alice', balance=100)
        usernames = ('bob', 'carl', 'dana', 'emma', 'fred', 'greg')
        users = [self.make_participant(username) for username in usernames]

        prev = alice
        for user in reversed(users):
            prev.set_tip_to(user, EUR('1.00'))
            prev = user

        def check():
            for username in reversed(usernames[1:]):
                user = Participant.from_username(username)
                assert user.giving == D('1.00')
                assert user.receiving == D('1.00')
                assert user.npatrons == 1
            funded_tips = self.db.all("SELECT id FROM tips WHERE is_funded ORDER BY id")
            assert len(funded_tips) == 6

        check()
        Payday.start().update_cached_amounts()
        check()

    @mock.patch('liberapay.billing.payday.log')
    def test_start_prepare(self, log):
        self.clear_tables()
        self.make_participant('carl', balance=10)

        payday = Payday.start()
        ts_start = payday.ts_start

        get_participants = lambda c: c.all("SELECT * FROM payday_participants")

        with self.db.get_cursor() as cursor:
            payday.prepare(cursor, ts_start)
            participants = get_participants(cursor)

        expected_logging_call_args = [
            ('Running payday #1.'),
            ('Payday started at {}.'.format(ts_start)),
            ('Prepared the DB.'),
        ]
        expected_logging_call_args.reverse()
        for args, _ in log.call_args_list:
            assert args[0] == expected_logging_call_args.pop()

        log.reset_mock()

        # run a second time, we should see it pick up the existing payday
        payday = Payday.start()
        second_ts_start = payday.ts_start
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor, second_ts_start)
            second_participants = get_participants(cursor)

        assert ts_start == second_ts_start
        participants = list(participants)
        second_participants = list(second_participants)

        # carl is the only participant
        assert len(participants) == 1
        assert participants == second_participants

        expected_logging_call_args = [
            ('Running payday #1.'),
            ('Payday started at {}.'.format(second_ts_start)),
            ('Prepared the DB.'),
        ]
        expected_logging_call_args.reverse()
        for args, _ in log.call_args_list:
            assert args[0] == expected_logging_call_args.pop()

    def test_end(self):
        Payday.start().end()
        result = self.db.one("SELECT count(*) FROM paydays "
                             "WHERE ts_end > '1970-01-01'")
        assert result == 1

    def test_end_raises_NoPayday(self):
        with self.assertRaises(NoPayday):
            Payday().end()

    def test_payday_cant_make_balances_more_negative(self):
        self.db.run("""
            UPDATE participants SET balance = -10 WHERE username='janet'
        """)
        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor, payday.ts_start)
            cursor.run("""
                UPDATE payday_participants
                   SET new_balance = -50
                 WHERE username IN ('janet', 'homer')
            """)
            with self.assertRaises(NegativeBalance):
                payday.check_balances(cursor)

    @staticmethod
    def get_new_balances(cursor):
        return {id: new_balance for id, new_balance in cursor.all(
            "SELECT id, new_balance FROM payday_participants"
        )}

    def test_payday_doesnt_process_tips_when_goal_is_negative(self):
        self.make_exchange('mango-cc', 20, 0, self.janet)
        self.janet.set_tip_to(self.homer, EUR('13.00'))
        self.db.run("UPDATE participants SET goal = -1 WHERE username='homer'")
        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor, payday.ts_start)
            payday.transfer_virtually(cursor, payday.ts_start)
            new_balances = self.get_new_balances(cursor)
            assert new_balances[self.janet.id] == 20
            assert new_balances[self.homer.id] == 0

    def test_payday_doesnt_make_null_transfers(self):
        alice = self.make_participant('alice')
        alice.set_tip_to(self.homer, EUR('1.00'))
        alice.set_tip_to(self.homer, EUR(0))
        a_team = self.make_participant('a_team', kind='group')
        a_team.add_member(alice)
        Payday.start().run()
        transfers0 = self.db.all("SELECT * FROM transfers WHERE amount = 0")
        assert not transfers0

    def test_transfer_tips(self):
        self.make_exchange('mango-cc', 1, 0, self.david)
        self.david.set_tip_to(self.janet, EUR('0.51'))
        self.david.set_tip_to(self.homer, EUR('0.50'))
        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor, payday.ts_start)
            payday.transfer_virtually(cursor, payday.ts_start)
            new_balances = self.get_new_balances(cursor)
            assert new_balances[self.david.id] == D('0.49')
            assert new_balances[self.janet.id] == D('0.51')
            assert new_balances[self.homer.id] == 0
            nulls = cursor.all("SELECT * FROM payday_tips WHERE is_funded IS NULL")
            assert not nulls

    def test_transfer_tips_whole_graph(self):
        alice = self.make_participant('alice', balance=50)
        alice.set_tip_to(self.homer, EUR('50'))
        self.homer.set_tip_to(self.janet, EUR('20'))
        self.janet.set_tip_to(self.david, EUR('5'))
        self.david.set_tip_to(self.homer, EUR('20'))  # Should be unfunded

        payday = Payday.start()
        with self.db.get_cursor() as cursor:
            payday.prepare(cursor, payday.ts_start)
            payday.transfer_virtually(cursor, payday.ts_start)
            new_balances = self.get_new_balances(cursor)
            assert new_balances[alice.id] == D('0')
            assert new_balances[self.homer.id] == D('30')
            assert new_balances[self.janet.id] == D('15')
            assert new_balances[self.david.id] == D('5')

    def test_transfer_takes(self):
        a_team = self.make_participant('a_team', kind='group')
        alice = self.make_participant('alice')
        a_team.set_take_for(alice, D('1.00'), a_team)
        bob = self.make_participant('bob')
        a_team.set_take_for(bob, D('0.01'), a_team)
        charlie = self.make_participant('charlie', balance=1000)
        charlie.set_tip_to(a_team, EUR('1.01'))

        payday = Payday.start()

        # Test that payday ignores takes set after it started
        a_team.set_take_for(alice, D('2.00'), a_team)

        # Run the transfer multiple times to make sure we ignore takes that
        # have already been processed
        with mock.patch.object(payday, 'transfer_for_real') as f:
            f.side_effect = Foobar
            with self.assertRaises(Foobar):
                payday.shuffle()
        for i in range(2):
            payday.shuffle()

        participants = self.db.all("SELECT username, balance FROM participants")

        for p in participants:
            if p.username == 'alice':
                assert p.balance == D('1.00')
            elif p.username == 'bob':
                assert p.balance == D('0.01')
            elif p.username == 'charlie':
                assert p.balance == D('998.99')
            else:
                assert p.balance == 0

    def test_underfunded_team(self):
        self.clear_tables()
        team = self.make_participant('team', kind='group')
        alice = self.make_participant('alice')
        team.set_take_for(alice, D('1.00'), team)
        bob = self.make_participant('bob')
        team.set_take_for(bob, D('1.00'), team)
        charlie = self.make_participant('charlie', balance=1000)
        charlie.set_tip_to(team, EUR('0.26'))

        Payday.start().run()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('0.13'),
            'bob': D('0.13'),
            'charlie': D('999.74'),
            'team': D('0.00'),
        }
        assert d == expected

    def test_wellfunded_team(self):
        """
        This tests two properties:
        - takes are maximums
        - donors all pay their share, the first donor doesn't pay everything
        """
        self.clear_tables()
        team = self.make_participant('team', kind='group')
        alice = self.make_participant('alice')
        team.set_take_for(alice, D('0.79'), team)
        bob = self.make_participant('bob')
        team.set_take_for(bob, D('0.21'), team)
        charlie = self.make_participant('charlie', balance=10)
        charlie.set_tip_to(team, EUR('5.00'))
        dan = self.make_participant('dan', balance=10)
        dan.set_tip_to(team, EUR('5.00'))

        Payday.start().run()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('0.79'),
            'bob': D('0.21'),
            'charlie': D('9.5'),
            'dan': D('9.5'),
            'team': D('0.00'),
        }
        assert d == expected

    def test_wellfunded_team_with_early_donor(self):
        self.clear_tables()
        team = self.make_participant('team', kind='group')
        alice = self.make_participant('alice')
        team.set_take_for(alice, D('0.79'), team)
        bob = self.make_participant('bob')
        team.set_take_for(bob, D('0.21'), team)
        charlie = self.make_participant('charlie', balance=10)
        charlie.set_tip_to(team, EUR('2.00'))

        print("> Step 1: three weeks of donations from charlie only")
        print()
        for i in range(3):
            Payday.start().run(recompute_stats=0, update_cached_amounts=False)
            print()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('0.79') * 3,
            'bob': D('0.21') * 3,
            'charlie': D('7.00'),
            'team': D('0.00'),
        }
        assert d == expected

        print("> Step 2: dan joins the party, charlie's donation is automatically "
              "reduced while dan catches up")
        print()
        dan = self.make_participant('dan', balance=10)
        dan.set_tip_to(team, EUR('2.00'))

        for i in range(6):
            Payday.start().run(recompute_stats=0, update_cached_amounts=False)
            print()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('0.79') * 9,
            'bob': D('0.21') * 9,
            'charlie': D('5.50'),
            'dan': D('5.50'),
            'team': D('0.00'),
        }
        assert d == expected

        print("> Step 3: dan has caught up with charlie, they will now both give 0.50")
        print()
        for i in range(3):
            Payday.start().run(recompute_stats=0, update_cached_amounts=False)
            print()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('0.79') * 12,
            'bob': D('0.21') * 12,
            'charlie': D('4.00'),
            'dan': D('4.00'),
            'team': D('0.00'),
        }
        assert d == expected

    def test_wellfunded_team_with_two_early_donors(self):
        self.clear_tables()
        team = self.make_participant('team', kind='group')
        alice = self.make_participant('alice')
        team.set_take_for(alice, D('0.79'), team)
        bob = self.make_participant('bob')
        team.set_take_for(bob, D('0.21'), team)
        charlie = self.make_participant('charlie', balance=10)
        charlie.set_tip_to(team, EUR('1.00'))
        dan = self.make_participant('dan', balance=10)
        dan.set_tip_to(team, EUR('3.00'))

        print("> Step 1: three weeks of donations from early donors")
        print()
        for i in range(3):
            Payday.start().run(recompute_stats=0, update_cached_amounts=False)
            print()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('0.79') * 3,
            'bob': D('0.21') * 3,
            'charlie': D('9.25'),
            'dan': D('7.75'),
            'team': D('0.00'),
        }
        assert d == expected

        print("> Step 2: a new donor appears, the contributions of the early "
              "donors automatically decrease while the new donor catches up")
        print()
        emma = self.make_participant('emma', balance=10)
        emma.set_tip_to(team, EUR('1.00'))

        Payday.start().run(recompute_stats=0, update_cached_amounts=False)
        print()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('0.79') * 4,
            'bob': D('0.21') * 4,
            'charlie': D('9.19'),
            'dan': D('7.59'),
            'emma': D('9.22'),
            'team': D('0.00'),
        }
        assert d == expected

        Payday.start().run(recompute_stats=0, update_cached_amounts=False)
        print()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('0.79') * 5,
            'bob': D('0.21') * 5,
            'charlie': D('8.99'),
            'dan': D('7.01'),
            'emma': D('9.00'),
            'team': D('0.00'),
        }
        assert d == expected

        print("> Step 3: emma has caught up with the early donors")
        print()

        for i in range(2):
            Payday.start().run(recompute_stats=0, update_cached_amounts=False)
            print()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('0.79') * 7,
            'bob': D('0.21') * 7,
            'charlie': D('8.60'),
            'dan': D('5.80'),
            'emma': D('8.60'),
            'team': D('0.00'),
        }
        assert d == expected

    def test_wellfunded_team_with_two_early_donors_and_low_amounts(self):
        self.clear_tables()
        team = self.make_participant('team', kind='group')
        alice = self.make_participant('alice')
        team.set_take_for(alice, D('0.01'), team)
        bob = self.make_participant('bob')
        team.set_take_for(bob, D('0.01'), team)
        charlie = self.make_participant('charlie', balance=10)
        charlie.set_tip_to(team, EUR('0.02'))
        dan = self.make_participant('dan', balance=10)
        dan.set_tip_to(team, EUR('0.02'))

        print("> Step 1: three weeks of donations from early donors")
        print()
        for i in range(3):
            Payday.start().run(recompute_stats=0, update_cached_amounts=False)
            print()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('0.01') * 3,
            'bob': D('0.01') * 3,
            'charlie': D('9.97'),
            'dan': D('9.97'),
            'team': D('0.00'),
        }
        assert d == expected

        print("> Step 2: a new donor appears, the contributions of the early "
              "donors automatically decrease while the new donor catches up")
        print()
        emma = self.make_participant('emma', balance=10)
        emma.set_tip_to(team, EUR('0.02'))

        for i in range(6):
            Payday.start().run(recompute_stats=0, update_cached_amounts=False)
            print()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('0.01') * 9,
            'bob': D('0.01') * 9,
            'charlie': D('9.94'),
            'dan': D('9.94'),
            'emma': D('9.94'),
            'team': D('0.00'),
        }
        assert d == expected

    def test_wellfunded_team_with_early_donor_and_small_leftover(self):
        self.clear_tables()
        team = self.make_participant('team', kind='group')
        alice = self.make_participant('alice')
        team.set_take_for(alice, D('0.50'), team)
        bob = self.make_participant('bob')
        team.set_take_for(bob, D('0.50'), team)
        charlie = self.make_participant('charlie', balance=10)
        charlie.set_tip_to(team, EUR('0.52'))

        print("> Step 1: three weeks of donations from early donor")
        print()
        for i in range(3):
            Payday.start().run(recompute_stats=0, update_cached_amounts=False)
            print()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('0.26') * 3,
            'bob': D('0.26') * 3,
            'charlie': D('8.44'),
            'team': D('0.00'),
        }
        assert d == expected

        print("> Step 2: a new donor appears, the contribution of the early "
              "donor automatically decreases while the new donor catches up, "
              "but the leftover is small so the adjustments are limited")
        print()
        dan = self.make_participant('dan', balance=10)
        dan.set_tip_to(team, EUR('0.52'))

        for i in range(3):
            Payday.start().run(recompute_stats=0, update_cached_amounts=False)
            print()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('0.26') * 3 + D('0.50') * 3,
            'bob': D('0.26') * 3 + D('0.50') * 3,
            'charlie': D('7.00'),
            'dan': D('8.44'),
            'team': D('0.00'),
        }
        assert d == expected

    def test_mutual_tipping_through_teams(self):
        self.clear_tables()
        team = self.make_participant('team', kind='group')
        alice = self.make_participant('alice', balance=8)
        alice.set_tip_to(team, EUR('2.00'))
        team.set_take_for(alice, D('0.25'), team)
        bob = self.make_participant('bob', balance=10)
        bob.set_tip_to(team, EUR('2.00'))
        team.set_take_for(bob, D('0.75'), team)

        Payday.start().run()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('7.75'),  # 8 - 0.50 + 0.25
            'bob': D('10.25'),  # 10 - 0.25 + 0.50
            'team': D('0.00'),
        }
        assert d == expected

    def test_unfunded_tip_to_team_doesnt_cause_NegativeBalance(self):
        self.clear_tables()
        team = self.make_participant('team', kind='group')
        alice = self.make_participant('alice')
        alice.set_tip_to(team, EUR('1.00'))  # unfunded tip
        bob = self.make_participant('bob')
        team.set_take_for(bob, D('1.00'), team)

        Payday.start().run()

        d = dict(self.db.all("SELECT username, balance FROM participants"))
        expected = {
            'alice': D('0.00'),
            'bob': D('0.00'),
            'team': D('0.00'),
        }
        assert d == expected

    def make_invoice(self, sender, addressee, amount, status):
        invoice_data = {
            'nature': 'expense',
            'amount': amount,
            'description': 'lorem ipsum',
            'details': '',
        }
        r = self.client.PxST(
            '/~%s/invoices/new' % addressee.id, auth_as=sender,
            data=invoice_data, xhr=True,
        )
        assert r.code == 200, r.text
        invoice_id = json.loads(r.text)['invoice_id']
        if status == 'pre':
            return invoice_id
        r = self.client.PxST(
            '/~%s/invoices/%s' % (addressee.id, invoice_id), auth_as=sender,
            data={'action': 'send'},
        )
        assert r.code == 302, r.text
        if status == 'new':
            return invoice_id
        r = self.client.PxST(
            '/~%s/invoices/%s' % (addressee.id, invoice_id), auth_as=addressee,
            data={'action': status[:-2], 'message': 'a message'},
        )
        assert r.code == 302, r.text
        return invoice_id

    def test_it_handles_invoices_correctly(self):
        org = self.make_participant('org', kind='organization', allow_invoices=True)
        self.make_exchange('mango-cc', 60, 0, self.janet)
        self.janet.set_tip_to(org, EUR('50.00'))
        self.db.run("UPDATE participants SET allow_invoices = true WHERE id = %s",
                    (self.janet.id,))
        self.make_invoice(self.janet, org, '40.02', 'accepted')
        self.make_invoice(self.janet, org, '80.04', 'accepted')
        self.make_invoice(self.janet, org, '5.16', 'rejected')
        self.make_invoice(self.janet, org, '3.77', 'new')
        self.make_invoice(self.janet, org, '1.23', 'pre')
        Payday.start().run()
        expense_transfers = self.db.all("SELECT * FROM transfers WHERE context = 'expense'")
        assert len(expense_transfers) == 1
        d = dict(self.db.all("SELECT username, balance FROM participants WHERE balance <> 0"))
        assert d == {
            'org': D('9.98'),
            'janet': D('50.02'),
        }

    def test_payday_tries_to_settle_debts(self):
        # First, test a small debt which can be settled
        e1_id = self.make_exchange('mango-cc', 10, 0, self.janet)
        debt = create_debt(self.db, self.janet.id, self.homer.id, 5, e1_id)
        e2_id = self.make_exchange('mango-cc', 20, 0, self.janet)
        Payday.start().run()
        balances = dict(self.db.all("SELECT username, balance FROM participants"))
        assert balances == {
            'janet': 25,
            'homer': 5,
            'david': 0,
        }
        debt = self.db.one("SELECT * FROM debts WHERE id = %s", (debt.id,))
        assert debt.status == 'paid'
        # Second, test a big debt that can't be settled
        self.make_exchange('mango-ba', -15, 0, self.janet)
        debt2 = create_debt(self.db, self.janet.id, self.homer.id, 20, e2_id)
        Payday.start().run()
        balances = dict(self.db.all("SELECT username, balance FROM participants"))
        assert balances == {
            'janet': 10,
            'homer': 5,
            'david': 0,
        }
        debt2 = self.db.one("SELECT * FROM debts WHERE id = %s", (debt2.id,))
        assert debt2.status == 'due'

    def test_it_notifies_participants(self):
        self.make_exchange('mango-cc', 10, 0, self.janet)
        self.janet.set_tip_to(self.david, EUR('4.50'))
        self.janet.set_tip_to(self.homer, EUR('3.50'))
        team = self.make_participant('team', kind='group', email='team@example.com')
        self.janet.set_tip_to(team, EUR('0.25'))
        team.add_member(self.david)
        team.set_take_for(self.david, D('0.23'), team)
        self.client.POST('/homer/emails/notifications.json', auth_as=self.homer,
                         data={'fields': 'income', 'income': ''}, xhr=True)
        kalel = self.make_participant(
            'kalel', mangopay_user_id=None, email='kalel@example.org',
        )
        self.janet.set_tip_to(kalel, EUR('0.10'))
        Payday.start().run()
        david = self.david.refetch()
        assert david.balance == D('4.73')
        janet = self.janet.refetch()
        assert janet.balance == D('1.77')
        assert janet.giving == D('0.25')
        emails = self.get_emails()
        assert len(emails) == 3
        assert emails[0]['to'][0] == 'david <%s>' % self.david.email
        assert '4.73' in emails[0]['subject']
        assert emails[1]['to'][0] == 'kalel <%s>' % kalel.email
        assert 'identity form' in emails[1]['text']
        assert emails[2]['to'][0] == 'janet <%s>' % self.janet.email
        assert 'top up' in emails[2]['subject']
        assert '1.77' in emails[2]['text']

    def test_log_upload(self):
        payday = Payday.start()
        with open('payday-%i.txt.part' % payday.id, 'w') as f:
            f.write('fake log file\n')
        with mock.patch.object(self.website, 's3') as s3:
            payday.run('.', keep_log=True)
            assert s3.upload_file.call_count == 1

    @mock.patch('liberapay.billing.payday.date')
    @mock.patch('liberapay.website.website.platforms.github.api_get')
    @mock.patch('liberapay.website.website.platforms.github.api_request')
    def test_create_payday_issue(self, api_request, api_get, date):
        date.today.return_value.isoweekday.return_value = 3
        # 1st payday issue
        api_get.return_value.json = lambda: []
        repo = self.website.app_conf.payday_repo
        html_url = 'https://github.com/%s/issues/1' % repo
        api_request.return_value.json = lambda: {'html_url': html_url}
        create_payday_issue()
        args = api_request.call_args
        post_path = '/repos/%s/issues' % repo
        assert args[0] == ('POST', '', post_path)
        assert args[1]['json'] == {'body': '', 'title': 'Payday #1', 'labels': ['Payday']}
        assert args[1]['sess'].auth
        public_log = self.db.one("SELECT public_log FROM paydays")
        assert public_log == html_url
        api_request.reset_mock()
        # Check that executing the function again doesn't create a duplicate
        create_payday_issue()
        assert api_request.call_count == 0
        # Run 1st payday
        Payday.start().run()
        # 2nd payday issue
        api_get.return_value.json = lambda: [{'body': 'Lorem ipsum', 'title': 'Payday #1'}]
        html_url = 'https://github.com/%s/issues/2' % repo
        api_request.return_value.json = lambda: {'html_url': html_url}
        create_payday_issue()
        args = api_request.call_args
        assert args[0] == ('POST', '', post_path)
        assert args[1]['json'] == {'body': 'Lorem ipsum', 'title': 'Payday #2', 'labels': ['Payday']}
        assert args[1]['sess'].auth
        public_log = self.db.one("SELECT public_log FROM paydays WHERE id = 2")
        assert public_log == html_url
