from swfactory.queue import branch_name, slugify


def test_names():
    assert slugify("Add a /health endpoint!") == "add-a-health-endpoint"
    assert branch_name(7, "x") == "factory/007-x"
    assert slugify("x" * 60) == "x" * 40
