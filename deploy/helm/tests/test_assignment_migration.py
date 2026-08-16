"""Release wiring gate for the assignment-ledger migration."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "helm" / "charts" / "vexa"


def test_assignment_migration_is_default_on_and_uses_a_real_module_entrypoint():
    values = (CHART / "values.yaml").read_text()
    job = (CHART / "templates" / "job-migrations.yaml").read_text()
    deployment = (CHART / "templates" / "deployment-meeting-api.yaml").read_text()

    migrations = values.split("\nmigrations:\n", 1)[1].split("\n# ----------", 1)[0]
    assert "enabled: true" in migrations
    assert "python -m meeting_api.database" in job
    assert 'command: ["python", "-m", "meeting_api.database"]' in deployment
    assert "initContainers:" in deployment
