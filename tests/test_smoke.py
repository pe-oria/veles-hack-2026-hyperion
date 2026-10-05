from veles.main import main


def test_main_runs(capsys):
    main()
    assert "environment OK" in capsys.readouterr().out
