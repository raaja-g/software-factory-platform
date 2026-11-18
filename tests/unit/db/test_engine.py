from swfactory.db.engine import make_engine, make_sessionmaker


def test_sessionmaker_keeps_objects():
    sm = make_sessionmaker(make_engine("postgresql+psycopg://u@h/db"))
    assert sm.kw["expire_on_commit"] is False
