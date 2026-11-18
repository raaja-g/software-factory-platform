from psycopg.rows import dict_row

from swfactory.db.engine import checkpointer_pool_kwargs


def test_pool_kwargs():
    assert checkpointer_pool_kwargs() == {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
        "options": "-c search_path=lg",
    }
