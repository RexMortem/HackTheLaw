"""Lock the Meridian quantum math.  Run:  python -m quantum.test_quantum"""
from .case_meridian import result
from .constraints import recoverable_under


def test_point_scenarios():
    r = result()
    s = r.scenarios
    # Supportable loss of profit is £1.3m (£4.2m − £2.9m causation).
    assert r.headline == 1_800_000 + 1_300_000 == 3_100_000
    # Defendant's best: profit excluded, total capped at the £1.8m charges paid.
    assert s["cap_and_exclusion_hold"] == 1_800_000
    # Claimant's best: fraud carve-out removes both limbs -> full supportable.
    assert s["fraud_proven"] == 3_100_000
    # Exclusion struck but cap stands -> still bounded by the cap.
    assert s["exclusion_struck_cap_holds"] == 1_800_000


def test_expected_weighted():
    # pf=.25, p_excl=.60, p_cap=.70, wasted=1.8m, lop=1.3m, cap=1.8m
    r = result()
    assert r.expected_recoverable == 2_242_000.0


def test_cost_basis_survives_exclusion():
    # Wasted expenditure is not loss of profit: an exclusion never zeroes it.
    assert recoverable_under(1_800_000, 1_300_000, 1_800_000,
                             excl_upheld=True, cap_upheld=False, fraud=False) == 1_800_000


if __name__ == "__main__":
    test_point_scenarios()
    test_expected_weighted()
    test_cost_basis_survives_exclusion()
    print("ok — all quantum assertions pass")
