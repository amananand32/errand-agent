import pytest

from evals.harness import run_scenario
from evals.scenarios import SCENARIOS
from tests.conftest import RESULTS


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s.id for s in SCENARIOS])
def test_scenario(sc):
    res = run_scenario(sc)
    RESULTS.append(res)
    if res.error:
        pytest.fail(res.error)
    failed = [c for c in res.checks if not c[1]]
    assert not failed, "\n".join(f"{n}: {d}" for n, ok, d in failed)
