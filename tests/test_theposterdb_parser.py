import pytest
from app.providers.theposterdb import ThePosterDBProvider

@pytest.mark.asyncio
async def test_tpdb_provider_uses_stable_asset_id():
    # The actual site is intentionally not hit in unit tests; this checks the URL contract used by the provider.
    assert ThePosterDBProvider.ASSET.endswith('/api/assets/')
