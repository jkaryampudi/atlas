from atlas.dcp.learning.drift import CusumDetector


def test_detects_sustained_underperformance():
    d = CusumDetector(k=0.5, h=5.0)
    breached = False
    for _ in range(30):
        breached = d.update(-1.0)   # persistent -1 sigma residual
        if breached:
            break
    assert breached


def test_no_false_fire_on_alternating_noise():
    d = CusumDetector(k=0.5, h=5.0)
    seq = [0.8, -0.8] * 50          # zero-mean noise within slack
    assert not any(d.update(x) for x in seq)


def test_breach_latches_until_reset():
    d = CusumDetector(k=0.0, h=1.0)
    d.update(2.0)
    assert d.update(0.0) is True
    d.reset()
    assert d.update(0.0) is False
