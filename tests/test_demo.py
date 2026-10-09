"""`cctripwire demo` runs a storm against a stand-in claude in a temp dir."""
from cctripwire import demo


def test_demo_storm_is_cut_off(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))      # proves nothing outside the temp dir is needed
    lines = []
    r = demo.run(n=60, sleep_s=1.0, out=lines.append)
    assert r["launched"] == 60 and r["other"] == 0
    assert r["ran"] <= 12 and r["refused"] >= 48
    assert any("breaker reset" in ln for ln in lines)
    assert not any(tmp_path.iterdir())                # left nothing behind in HOME
