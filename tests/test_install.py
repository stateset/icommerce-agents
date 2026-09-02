import pytest


def test_upstream_submodule_is_checked_out():
    from engine_backend import SKILLS_DIR, UPSTREAM_ROOT

    assert (UPSTREAM_ROOT / "commerce-common" / "commerce_common" / "fencing.py").is_file()
    assert SKILLS_DIR("shopping").is_dir()
    assert SKILLS_DIR("merchant").is_dir()
    with pytest.raises(ValueError):
        SKILLS_DIR("nope")


def test_both_worlds_import():
    import commerce_common  # upstream shared layer
    import stateset_embedded  # the engine
    from merchant_agent.backend import MerchantBackend
    from shopping_agent.backend import StorefrontBackend

    assert stateset_embedded.Commerce is not None
    assert commerce_common is not None
    assert StorefrontBackend is not None and MerchantBackend is not None


def test_engine_opens_in_memory():
    from stateset_embedded import Commerce

    commerce = Commerce(":memory:")
    assert commerce.products.count() == 0
