from app.database import validate_alter_table_column


def test_sql_dynamic_whitelist_allows_known_research_columns():
    assert validate_alter_table_column("signal_observations", "score_bucket", "TEXT")
    assert validate_alter_table_column("signal_path_metrics", "source", "TEXT")


def test_sql_dynamic_whitelist_blocks_unknown_column():
    assert not validate_alter_table_column("signal_observations", "evil_column", "TEXT")
    assert not validate_alter_table_column("signal_path_metrics", "source", "TEXT; DROP TABLE trades")
