"""P1 DoD：`stocklab db init` 能在空目录建库。"""

from stocklab.cli.main import main
from stocklab.store.db import connect


def test_db_init_creates_database(tmp_db, capsys):
    assert not tmp_db.exists()
    rc = main(["db", "init", "--db", str(tmp_db)])
    assert rc == 0
    assert tmp_db.exists()
    with connect(tmp_db) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='bars_daily'"
        ).fetchone()[0]
    assert n == 1
    assert "首次建库" in capsys.readouterr().out


def test_db_init_rerun_is_idempotent_and_backs_up(tmp_db, tmp_path, capsys):
    main(["db", "init", "--db", str(tmp_db)])
    rc = main(["db", "init", "--db", str(tmp_db)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "备份" in out
    backups = list(tmp_path.glob("backups/*.db"))
    assert len(backups) == 1
    assert backups[0].exists()


def test_unknown_command_returns_2(tmp_db):
    assert main(["nope"]) == 2


def test_missing_subcommand_returns_2():
    assert main([]) == 2
